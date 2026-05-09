from __future__ import annotations

from pathlib import Path

from symphony.config import (
    AgentConfig,
    ClaudeConfig,
    GitHubConfig,
    PollingConfig,
    RetryConfig,
    RoleConfig,
    RoleGraphConfig,
    RoleStateConfig,
    RoleTransitionConfig,
    TrackerConfig,
    WorkflowConfig,
    WorkspaceConfig,
)
from symphony.evidence import (
    DECIDED_BY_DETECTOR,
    OUTCOME_COMPLETED_NO_PR_DECLARED,
    OUTCOME_COMPLETED_WITH_PR,
    DetectorResult,
)
from symphony.github.tracker import FakeGitHubTracker
from symphony.models import Issue
from symphony.orchestrator import Orchestrator
from symphony.provider.fake import FakeProvider
from symphony.role_graph import TransitionPlan, plan_claim, plan_transition, resolve_issue_state
from symphony.workspace import WorkspaceManager


class _StubDetector:
    def __init__(self, outcome: str, *, no_pr_reason: str | None = None) -> None:
        self.outcome = outcome
        self.no_pr_reason = no_pr_reason

    def detect(self, **_kwargs) -> DetectorResult:
        evidence = []
        if self.outcome == OUTCOME_COMPLETED_WITH_PR:
            evidence.append(
                {
                    "type": "pr_linked",
                    "url": "https://github.com/acme/proj/pull/10",
                    "number": 10,
                }
            )
        return DetectorResult(
            task_outcome=self.outcome,
            task_evidence=evidence,
            no_pr_reason=self.no_pr_reason,
            outcome_decided_by=DECIDED_BY_DETECTOR,
        )


def _issue() -> Issue:
    return Issue(
        id="I_1",
        number=1,
        identifier="acme/proj#1",
        owner="acme",
        repo="proj",
        title="Role workflow",
        body="Body",
        state="open",
        url="https://github.com/acme/proj/issues/1",
        labels=("symphony-ready-impl",),
    )


def _config(tmp_path: Path, graph: RoleGraphConfig) -> WorkflowConfig:
    return WorkflowConfig(
        tracker=TrackerConfig(
            kind="github",
            owner="acme",
            repo="proj",
            token="literal-token",
            include_labels=(),
            exclude_labels=("symphony-done",),
        ),
        agent=AgentConfig(max_concurrency=1, max_turns=1),
        workspace=WorkspaceConfig(root=tmp_path / "ws"),
        claude=ClaudeConfig(
            model="fake-model",
            permission_mode="acceptEdits",
            session_store=tmp_path / "sessions",
            transcript_store=tmp_path / "transcripts",
            artifact_store=tmp_path / "artifacts",
        ),
        github=GitHubConfig(
            ready_label="symphony-ready-impl",
            claim_label="symphony-implementing",
            blocked_label="symphony-blocked-operator",
        ),
        polling=PollingConfig(),
        retry=RetryConfig(),
        role_graph=graph,
        workflow_path=tmp_path / "WORKFLOW.md",
    )


def _graph() -> RoleGraphConfig:
    return RoleGraphConfig(
        roles={
            "implementer": RoleConfig(
                name="implementer",
                actor="agent",
                provider="claude_code",
                can_claim=("ready_impl", "changes_requested"),
                claim_state="implementing",
            ),
            "reviewer": RoleConfig(
                name="reviewer",
                actor="human",
                can_claim=("ready_review",),
                claim_state="reviewing",
            ),
            "leader": RoleConfig(
                name="leader",
                actor="human",
                can_claim=("needs_design",),
                claim_state="leader_reviewing",
            ),
        },
        states={
            "ready_impl": RoleStateConfig("ready_impl", ("symphony-ready-impl",)),
            "implementing": RoleStateConfig("implementing", ("symphony-implementing",)),
            "ready_review": RoleStateConfig("ready_review", ("symphony-ready-review",)),
            "reviewing": RoleStateConfig("reviewing", ("symphony-reviewing",)),
            "changes_requested": RoleStateConfig(
                "changes_requested",
                ("symphony-changes-requested",),
            ),
            "needs_design": RoleStateConfig(
                "needs_design",
                ("symphony-needs-design",),
                gate_owner="leader",
            ),
            "leader_reviewing": RoleStateConfig(
                "leader_reviewing",
                ("symphony-leader-reviewing",),
            ),
            "approved": RoleStateConfig("approved", ("symphony-approved",)),
            "done": RoleStateConfig("done", ("symphony-done",), terminal=True),
        },
        transitions={
            "pr_delivered": RoleTransitionConfig(
                "pr_delivered",
                "implementer",
                ("implementing",),
                "ready_review",
                ("pr_link",),
            ),
            "design_needed": RoleTransitionConfig(
                "design_needed",
                "implementer",
                ("implementing",),
                "needs_design",
                ("issue_comment",),
            ),
            "changes_requested": RoleTransitionConfig(
                "changes_requested",
                "reviewer",
                ("reviewing",),
                "changes_requested",
                ("review_comment",),
            ),
            "approved": RoleTransitionConfig(
                "approved",
                "reviewer",
                ("reviewing",),
                "approved",
                ("pr_approval",),
            ),
            "decision_to_impl": RoleTransitionConfig(
                "decision_to_impl",
                "leader",
                ("leader_reviewing",),
                "ready_impl",
                ("decision_comment",),
            ),
        },
    )


def _orchestrator(
    tmp_path: Path,
    tracker: FakeGitHubTracker,
    graph: RoleGraphConfig,
    detector: _StubDetector,
) -> Orchestrator:
    cfg = _config(tmp_path, graph)
    return Orchestrator(
        cfg,
        tracker=tracker,
        provider=FakeProvider(),
        workspace_manager=WorkspaceManager(cfg.workspace),
        evidence_detector=detector,
    )


def _apply_human_transition(
    tracker: FakeGitHubTracker,
    graph: RoleGraphConfig,
    *,
    issue_id: str,
    role_name: str,
    transition_name: str,
    evidence: tuple[str, ...],
) -> None:
    issue = tracker.states[issue_id].issue
    claim = plan_claim(graph, resolve_issue_state(graph, issue))
    assert isinstance(claim, TransitionPlan)
    tracker.apply_transition_plan(issue, claim, evidence_summary=f"{role_name} claimed")
    issue = tracker.states[issue_id].issue
    plan = plan_transition(
        graph,
        role_name=role_name,
        transition_name=transition_name,
        from_state_name=claim.to_state.name,
        evidence=evidence,
    )
    assert isinstance(plan, TransitionPlan)
    tracker.apply_transition_plan(issue, plan, evidence_summary=f"{role_name} decision")


async def test_fake_role_e2e_review_loop_returns_to_implementer_then_approval(
    tmp_path: Path,
) -> None:
    graph = _graph()
    issue = _issue()
    tracker = FakeGitHubTracker(issues=[issue], ready_label="")

    orch = _orchestrator(
        tmp_path,
        tracker,
        graph,
        _StubDetector(OUTCOME_COMPLETED_WITH_PR),
    )
    await orch.run_once()
    assert tracker.states[issue.identifier].issue.labels == ("symphony-ready-review",)
    await orch.run_once()
    assert orch.status_snapshot()["waiting_items"][0]["role"] == "reviewer"

    _apply_human_transition(
        tracker,
        graph,
        issue_id=issue.identifier,
        role_name="reviewer",
        transition_name="changes_requested",
        evidence=("review_comment",),
    )
    assert tracker.states[issue.identifier].issue.labels == ("symphony-changes-requested",)

    await orch.run_once()
    assert tracker.states[issue.identifier].issue.labels == ("symphony-ready-review",)

    _apply_human_transition(
        tracker,
        graph,
        issue_id=issue.identifier,
        role_name="reviewer",
        transition_name="approved",
        evidence=("pr_approval",),
    )
    assert tracker.states[issue.identifier].issue.labels == ("symphony-approved",)


async def test_fake_role_e2e_design_gate_returns_to_implementer(tmp_path: Path) -> None:
    graph = _graph()
    issue = _issue()
    tracker = FakeGitHubTracker(issues=[issue], ready_label="")
    orch = _orchestrator(
        tmp_path,
        tracker,
        graph,
        _StubDetector(
            OUTCOME_COMPLETED_NO_PR_DECLARED,
            no_pr_reason="design proposed",
        ),
    )

    await orch.run_once()

    assert tracker.states[issue.identifier].issue.labels == ("symphony-needs-design",)
    await orch.run_once()
    assert orch.status_snapshot()["waiting_items"][0]["gate_owner"] == "leader"

    _apply_human_transition(
        tracker,
        graph,
        issue_id=issue.identifier,
        role_name="leader",
        transition_name="decision_to_impl",
        evidence=("decision_comment",),
    )
    assert tracker.states[issue.identifier].issue.labels == ("symphony-ready-impl",)
