"""Routing tests for #62 (M5.3): task_outcome → mark_issue_blocked vs release.

Drives the orchestrator with a stub :class:`EvidenceDetector` injected
so each test pins the ``task_outcome`` independently of GitHub. The
routing rule under test is:

- ``incomplete_no_evidence`` → ``mark_issue_blocked``
- ``incomplete_permission_denied`` → ``mark_issue_blocked``
- ``completed_with_pr`` → ``release_issue``
- ``completed_no_pr_declared`` → ``release_issue``
- ``unknown`` → ``release_issue`` (we don't escalate runs we couldn't verify)

Other task outcomes (``blocked_operator_required`` / ``retryable_failure``)
are produced by the SPEC §17.4 derivation path for non-COMPLETED
provider states; they ride the existing ``non_retryable_failure`` /
``retryable`` routing and are covered in ``test_timeouts.py``.
"""

from __future__ import annotations

from pathlib import Path

from symphony.config import (
    AgentConfig,
    ClaudeConfig,
    GitHubConfig,
    LoggingConfig,
    PollingConfig,
    RetryConfig,
    TrackerConfig,
    WorkflowConfig,
    WorkspaceConfig,
)
from symphony.evidence import (
    DECIDED_BY_DETECTOR,
    OUTCOME_COMPLETED_NO_PR_DECLARED,
    OUTCOME_COMPLETED_WITH_PR,
    OUTCOME_INCOMPLETE_NO_EVIDENCE,
    OUTCOME_INCOMPLETE_PERMISSION_DENIED,
    OUTCOME_UNKNOWN,
    DetectorResult,
)
from symphony.github.tracker import FakeGitHubTracker
from symphony.models import Issue
from symphony.orchestrator import Orchestrator
from symphony.provider.fake import FakeProvider
from symphony.workspace import WorkspaceManager

# -- Helpers -----------------------------------------------------------------


def _issue(*, number: int = 1) -> Issue:
    return Issue(
        id=f"I_{number}",
        number=number,
        identifier=f"acme/proj#{number}",
        owner="acme",
        repo="proj",
        title=f"t{number}",
        body="b",
        state="open",
        url=f"https://github.com/acme/proj/issues/{number}",
        labels=("symphony-ready",),
    )


def _config(tmp_path: Path) -> WorkflowConfig:
    return WorkflowConfig(
        tracker=TrackerConfig(
            kind="github",
            owner="acme",
            repo="proj",
            token="literal-token",
            include_labels=("symphony-ready",),
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
        github=GitHubConfig(),
        polling=PollingConfig(),
        retry=RetryConfig(),
        logging=LoggingConfig(),
        workflow_path=tmp_path / "WORKFLOW.md",
    )


class _StubDetector:
    """EvidenceDetector test double that returns a fixed DetectorResult.

    Mirrors the real detector's ``detect(...)`` signature loosely (only
    the kwargs the orchestrator actually passes). Lets each test pin
    ``task_outcome`` and verify the resulting routing decision without
    needing a real GitHubClient or workspace.
    """

    def __init__(
        self,
        outcome: str,
        *,
        evidence: list | None = None,
        no_pr_reason: str | None = None,
    ) -> None:
        self._outcome = outcome
        self._evidence = list(evidence or [])
        self._no_pr_reason = no_pr_reason
        self.calls: list[dict] = []

    def detect(self, **kwargs) -> DetectorResult:
        self.calls.append(kwargs)
        return DetectorResult(
            task_outcome=self._outcome,
            task_evidence=self._evidence,
            no_pr_reason=self._no_pr_reason,
            outcome_decided_by=DECIDED_BY_DETECTOR,
        )


def _make_orch(
    tmp_path: Path,
    outcome: str,
    *,
    evidence: list | None = None,
    no_pr_reason: str | None = None,
) -> tuple[Orchestrator, FakeGitHubTracker]:
    cfg = _config(tmp_path)
    tracker = FakeGitHubTracker(issues=[_issue()])
    mgr = WorkspaceManager(cfg.workspace)
    detector = _StubDetector(outcome, evidence=evidence, no_pr_reason=no_pr_reason)
    orch = Orchestrator(
        cfg,
        tracker=tracker,
        provider=FakeProvider(),
        workspace_manager=mgr,
        evidence_detector=detector,
    )
    return orch, tracker


# -- Block routing on misleading-success (#62) ------------------------------


async def test_incomplete_no_evidence_marks_blocked(tmp_path: Path) -> None:
    """COMPLETED + detector says `incomplete_no_evidence` → mark_issue_blocked.

    This is the load-bearing case for #62: the leader's E2E #42 produced
    exactly this outcome (Claude answered, no PR materialized) and the
    orchestrator silently released the claim. Now it blocks instead.
    """
    orch, tracker = _make_orch(tmp_path, OUTCOME_INCOMPLETE_NO_EVIDENCE)
    await orch.run_once()

    state = tracker.states["acme/proj#1"]
    assert state.blocked is True
    assert state.claimed_by is None
    # Block reason mentions task_outcome so operators triaging
    # tracker history can see why.
    assert any(
        "task_outcome=incomplete_no_evidence" in entry[1]
        for entry in state.claim_history
    )
    assert len(state.progress_comments) == 1
    comment = state.progress_comments[0]
    assert "symphony:blocked-outcome" in comment
    assert "task_outcome: `incomplete_no_evidence`" in comment
    assert "terminal_state: `completed`" in comment
    assert "artifacts:" in comment
    assert "Required operator action" in comment


async def test_incomplete_permission_denied_marks_blocked(tmp_path: Path) -> None:
    """COMPLETED + detector says `incomplete_permission_denied` → blocked.

    Mirrors the leader E2E case where `permission_mode: acceptEdits`
    denied Bash mid-run and Claude fell back to a clarification message.
    Auto-retry would just spin under the same permission_mode."""
    orch, tracker = _make_orch(
        tmp_path,
        OUTCOME_INCOMPLETE_PERMISSION_DENIED,
        evidence=[
            {
                "type": "permission_denied",
                "denials_count": 1,
                "tool_names": ["AskUserQuestion", "ghp_12345678901234567890"],
            }
        ],
    )
    await orch.run_once()

    state = tracker.states["acme/proj#1"]
    assert state.blocked is True
    assert state.claimed_by is None
    assert any(
        "task_outcome=incomplete_permission_denied" in entry[1]
        for entry in state.claim_history
    )
    assert len(state.progress_comments) == 1
    comment = state.progress_comments[0]
    assert "permission_denied: denials_count=1" in comment
    assert "AskUserQuestion" in comment
    assert "ghp_12345678901234567890" not in comment
    assert "<redacted>" in comment


async def test_block_comment_failure_does_not_prevent_blocking(tmp_path: Path) -> None:
    orch, tracker = _make_orch(tmp_path, OUTCOME_INCOMPLETE_NO_EVIDENCE)

    def _fail_comment(_issue: Issue, _body: str) -> None:
        raise RuntimeError("comment failed")

    tracker.create_or_update_progress_comment = _fail_comment  # type: ignore[method-assign]

    await orch.run_once()

    state = tracker.states["acme/proj#1"]
    assert state.blocked is True
    assert state.claimed_by is None


# -- Release routing for clean / unverifiable outcomes (#62) ----------------


async def test_completed_with_pr_dequeues_without_marking_done(tmp_path: Path) -> None:
    """PR delivery leaves ready queue but does not imply issue completion."""
    orch, tracker = _make_orch(tmp_path, OUTCOME_COMPLETED_WITH_PR)
    await orch.run_once()

    state = tracker.states["acme/proj#1"]
    assert state.blocked is False
    assert state.claimed_by is None
    assert "symphony-ready" not in state.issue.labels
    assert "symphony-running" not in state.issue.labels
    assert "symphony-done" not in state.issue.labels
    # Claim history records queue removal, not issue completion.
    assert any("dequeue:" in entry[1] for entry in state.claim_history)
    assert all("done:" not in entry[1] for entry in state.claim_history)
    assert all("blocked:" not in entry[1] for entry in state.claim_history)
    assert state.progress_comments == []


async def test_completed_no_pr_declared_marks_done_and_removes_ready(
    tmp_path: Path,
) -> None:
    """Clean no-PR declarations leave the ready set instead of being reclaimed."""
    orch, tracker = _make_orch(
        tmp_path,
        OUTCOME_COMPLETED_NO_PR_DECLARED,
        no_pr_reason="already fixed by another PR",
    )
    await orch.run_once()

    state = tracker.states["acme/proj#1"]
    assert state.blocked is False
    assert state.claimed_by is None
    assert "symphony-ready" not in state.issue.labels
    assert "symphony-running" not in state.issue.labels
    assert "symphony-done" in state.issue.labels


async def test_completed_no_pr_design_proposed_marks_blocked_and_removes_ready(
    tmp_path: Path,
) -> None:
    """Design proposals need human approval, so they must not be reclaimed."""
    orch, tracker = _make_orch(
        tmp_path,
        OUTCOME_COMPLETED_NO_PR_DECLARED,
        no_pr_reason="design proposed",
    )
    await orch.run_once()

    state = tracker.states["acme/proj#1"]
    assert state.blocked is True
    assert state.claimed_by is None
    assert "symphony-ready" not in state.issue.labels
    assert "symphony-running" not in state.issue.labels
    assert "symphony-blocked" in state.issue.labels
    assert "symphony-done" not in state.issue.labels
    assert len(state.progress_comments) == 1
    assert "no_pr_reason=design proposed" in state.progress_comments[0]


async def test_unknown_outcome_releases_claim(tmp_path: Path) -> None:
    """`unknown` (detector couldn't verify) → release_issue.

    Critical for backward compat: existing test fakes use
    FakeGitHubTracker with no `.client`, which makes the detector
    return `unknown`. Routing must NOT escalate those runs to blocked
    or every prior orchestrator test would fail."""
    orch, tracker = _make_orch(tmp_path, OUTCOME_UNKNOWN)
    await orch.run_once()

    state = tracker.states["acme/proj#1"]
    assert state.blocked is False
    assert state.claimed_by is None


# -- terminal.json reflects the unified `blocked` decision ------------------


async def test_terminal_json_blocked_field_reflects_completion_block(tmp_path: Path) -> None:
    """When #62 escalates a misleading-success run, `terminal.json`'s
    `blocked` field MUST be True even though `terminal_state` is
    ``completed`` (not ``failed``). Previously `blocked` was tied to
    `non_retryable_failure` only — the new routing unifies it."""
    import json

    orch, _tracker = _make_orch(tmp_path, OUTCOME_INCOMPLETE_NO_EVIDENCE)
    await orch.run_once()

    terminal_files = list((tmp_path / "artifacts").rglob("terminal.json"))
    assert terminal_files
    record = json.loads(terminal_files[0].read_text())
    assert record["terminal_state"] == "completed"
    assert record["blocked"] is True
    assert record["task_outcome"] == OUTCOME_INCOMPLETE_NO_EVIDENCE


async def test_terminal_json_blocked_false_for_clean_completion(tmp_path: Path) -> None:
    import json

    orch, _tracker = _make_orch(tmp_path, OUTCOME_COMPLETED_WITH_PR)
    await orch.run_once()

    terminal_files = list((tmp_path / "artifacts").rglob("terminal.json"))
    record = json.loads(terminal_files[0].read_text())
    assert record["terminal_state"] == "completed"
    assert record["blocked"] is False
    assert record["task_outcome"] == OUTCOME_COMPLETED_WITH_PR


async def test_terminal_json_blocked_false_for_unknown_outcome(tmp_path: Path) -> None:
    """Unverifiable runs (no GitHubClient) MUST NOT show blocked=True
    in terminal.json — they're recorded as `unknown` and released."""
    import json

    orch, _tracker = _make_orch(tmp_path, OUTCOME_UNKNOWN)
    await orch.run_once()

    terminal_files = list((tmp_path / "artifacts").rglob("terminal.json"))
    record = json.loads(terminal_files[0].read_text())
    assert record["blocked"] is False
    assert record["task_outcome"] == OUTCOME_UNKNOWN


# -- PR-lookup-failure must not falsely block (#62 leader correction) -------


async def test_pr_lookup_failure_does_not_block_issue(tmp_path: Path) -> None:
    """End-to-end check for the #62 leader correction: a transient
    GitHubError during PR detection MUST classify the run as `unknown`
    and release the claim — NOT mark_issue_blocked.

    Drives the real :class:`EvidenceDetector` (no stub) with a
    GitHubClient that always raises 500. Verifies:

    1. tracker is NOT marked blocked
    2. claim is released
    3. terminal.json carries `task_outcome=unknown` and `blocked=false`

    Defends against a regression that conflates API failure with
    verified-no-PR.
    """
    import json

    import httpx

    from symphony.evidence import EvidenceDetector
    from symphony.github.client import GitHubClient

    def _always_500(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"message": "boom"})

    client = GitHubClient("ghp_test", transport=httpx.MockTransport(_always_500))
    cfg = _config(tmp_path)
    tracker = FakeGitHubTracker(issues=[_issue()])
    mgr = WorkspaceManager(cfg.workspace)
    # Wire the REAL detector (not a stub) so we exercise the
    # GitHubError → None code path that this test was added to lock in.
    detector = EvidenceDetector(cfg.github, client=client)
    orch = Orchestrator(
        cfg,
        tracker=tracker,
        provider=FakeProvider(),
        workspace_manager=mgr,
        evidence_detector=detector,
    )
    await orch.run_once()

    state = tracker.states["acme/proj#1"]
    # Critical assertion: PR-lookup failure does NOT cause blocking.
    assert state.blocked is False, (
        "Transient GitHub failure must NOT mark the issue blocked. "
        "If this fires, `_detect_pull_requests` likely returned [] on "
        "GitHubError instead of None — see leader correction on PR #73."
    )
    assert state.claimed_by is None  # released

    terminal_files = list((tmp_path / "artifacts").rglob("terminal.json"))
    record = json.loads(terminal_files[0].read_text())
    assert record["blocked"] is False
    assert record["task_outcome"] == OUTCOME_UNKNOWN
