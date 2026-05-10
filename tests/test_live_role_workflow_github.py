"""Opt-in live GitHub E2E for role workflow transitions.

Skipped by default. Enabled when both:

- ``SYMPHONY_RUN_ROLE_GITHUB_E2E=1``
- ``GITHUB_TOKEN`` is non-empty.

This test intentionally uses the real GitHub tracker with a fake provider and
stub evidence detector. It validates the GitHub boundary for the role workflow
chain: issue label setup -> role claim transition comment -> terminal
``pr_delivered`` transition comment -> ready-review label.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from symphony.cli import STANDARD_LABELS
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
    OUTCOME_COMPLETED_WITH_PR,
    DetectorResult,
)
from symphony.github import GitHubTracker
from symphony.github.client import GitHubClaimConflict, GitHubClient
from symphony.orchestrator import Orchestrator
from symphony.provider.fake import FakeProvider
from symphony.workspace import WorkspaceManager

_GATE_ENV = "SYMPHONY_RUN_ROLE_GITHUB_E2E"


def _gate() -> None:
    if os.environ.get(_GATE_ENV) != "1":
        pytest.skip(f"{_GATE_ENV} not set; live role workflow GitHub E2E skipped")
    if not os.environ.get("GITHUB_TOKEN"):
        pytest.skip("GITHUB_TOKEN not set; live role workflow GitHub E2E skipped")


class _CompletedWithPrDetector:
    def detect(self, **_kwargs) -> DetectorResult:
        return DetectorResult(
            task_outcome=OUTCOME_COMPLETED_WITH_PR,
            task_evidence=[
                {
                    "type": "pr_linked",
                    "url": "https://github.com/acme/proj/pull/999999",
                    "number": 999999,
                }
            ],
            outcome_decided_by=DECIDED_BY_DETECTOR,
        )


@pytest.mark.asyncio
async def test_live_role_workflow_claim_and_pr_handoff(tmp_path: Path) -> None:
    _gate()
    owner = os.environ.get("SYMPHONY_GITHUB_TEST_OWNER", "jimoosciuc")
    repo = os.environ.get("SYMPHONY_GITHUB_TEST_REPO", "symphony-cc")
    token = os.environ["GITHUB_TOKEN"]

    with GitHubClient(token) as client:
        _ensure_role_labels(client, owner=owner, repo=repo)
        raw_issue = client.post(
            f"/repos/{owner}/{repo}/issues",
            json_body={
                "title": "symphony live role workflow e2e",
                "body": "Temporary issue created by opt-in Symphony role workflow E2E.",
                "labels": ["symphony-ready-impl"],
            },
        )
        issue_number = int(raw_issue["number"])

        try:
            cfg = _config(tmp_path, owner=owner, repo=repo, token=token)
            tracker = GitHubTracker(cfg.tracker, cfg.github)
            try:
                orch = Orchestrator(
                    cfg,
                    tracker=tracker,
                    provider=FakeProvider(),
                    workspace_manager=WorkspaceManager(cfg.workspace),
                    evidence_detector=_CompletedWithPrDetector(),
                    workflow_reloader=None,
                )

                result = await orch.run_once()

                assert result.dispatched == [f"{owner}/{repo}#{issue_number}"]
                fresh = tracker.fetch_issues_by_numbers([issue_number])[0]
                assert "symphony-ready-review" in fresh.labels
                assert "symphony-ready-impl" not in fresh.labels
                assert "symphony-implementing" not in fresh.labels
                comments = client.get(f"/repos/{owner}/{repo}/issues/{issue_number}/comments")
                bodies = "\n".join(comment.get("body", "") for comment in comments)
                assert "transition: `claim:implementer`" in bodies
                assert "transition: `pr_delivered`" in bodies
            finally:
                tracker.close()
        finally:
            client.patch(
                f"/repos/{owner}/{repo}/issues/{issue_number}",
                json_body={"state": "closed"},
            )


def _ensure_role_labels(client: GitHubClient, *, owner: str, repo: str) -> None:
    for name in ("symphony-ready-impl", "symphony-implementing", "symphony-ready-review"):
        label = STANDARD_LABELS[name]
        path = f"/repos/{owner}/{repo}/labels"
        try:
            client.post(path, json_body=label)
        except GitHubClaimConflict:
            client.patch(f"{path}/{label['name']}", json_body=label)


def _config(tmp_path: Path, *, owner: str, repo: str, token: str) -> WorkflowConfig:
    return WorkflowConfig(
        tracker=TrackerConfig(
            kind="github",
            owner=owner,
            repo=repo,
            token=token,
            include_labels=("symphony-ready-impl",),
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
            done_label="symphony-done",
        ),
        polling=PollingConfig(),
        retry=RetryConfig(),
        role_graph=_graph(),
        workflow_path=tmp_path / "WORKFLOW.md",
    )


def _graph() -> RoleGraphConfig:
    return RoleGraphConfig(
        roles={
            "implementer": RoleConfig(
                name="implementer",
                actor="agent",
                provider="claude_code",
                can_claim=("ready_impl",),
                claim_state="implementing",
            ),
            "reviewer": RoleConfig(
                name="reviewer",
                actor="human",
                can_claim=("ready_review",),
                claim_state="reviewing",
            ),
        },
        states={
            "ready_impl": RoleStateConfig("ready_impl", ("symphony-ready-impl",)),
            "implementing": RoleStateConfig("implementing", ("symphony-implementing",)),
            "ready_review": RoleStateConfig("ready_review", ("symphony-ready-review",)),
            "reviewing": RoleStateConfig("reviewing", ("symphony-reviewing",)),
            "done": RoleStateConfig("done", ("symphony-done",), terminal=True),
        },
        transitions={
            "pr_delivered": RoleTransitionConfig(
                "pr_delivered",
                "implementer",
                ("implementing",),
                "ready_review",
                ("pr_link",),
            )
        },
    )
