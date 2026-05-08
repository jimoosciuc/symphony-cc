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

    def __init__(self, outcome: str, *, evidence: list | None = None) -> None:
        self._outcome = outcome
        self._evidence = list(evidence or [])
        self.calls: list[dict] = []

    def detect(self, **kwargs) -> DetectorResult:
        self.calls.append(kwargs)
        return DetectorResult(
            task_outcome=self._outcome,
            task_evidence=self._evidence,
            outcome_decided_by=DECIDED_BY_DETECTOR,
        )


def _make_orch(
    tmp_path: Path, outcome: str
) -> tuple[Orchestrator, FakeGitHubTracker]:
    cfg = _config(tmp_path)
    tracker = FakeGitHubTracker(issues=[_issue()])
    mgr = WorkspaceManager(cfg.workspace)
    detector = _StubDetector(outcome)
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


async def test_incomplete_permission_denied_marks_blocked(tmp_path: Path) -> None:
    """COMPLETED + detector says `incomplete_permission_denied` → blocked.

    Mirrors the leader E2E case where `permission_mode: acceptEdits`
    denied Bash mid-run and Claude fell back to a clarification message.
    Auto-retry would just spin under the same permission_mode."""
    orch, tracker = _make_orch(tmp_path, OUTCOME_INCOMPLETE_PERMISSION_DENIED)
    await orch.run_once()

    state = tracker.states["acme/proj#1"]
    assert state.blocked is True
    assert state.claimed_by is None
    assert any(
        "task_outcome=incomplete_permission_denied" in entry[1]
        for entry in state.claim_history
    )


# -- Release routing for clean / unverifiable outcomes (#62) ----------------


async def test_completed_with_pr_releases_claim(tmp_path: Path) -> None:
    """`completed_with_pr` → release_issue (clean success, label cleared)."""
    orch, tracker = _make_orch(tmp_path, OUTCOME_COMPLETED_WITH_PR)
    await orch.run_once()

    state = tracker.states["acme/proj#1"]
    assert state.blocked is False
    assert state.claimed_by is None
    # Claim history records the release, not a block.
    assert any("release:" in entry[1] for entry in state.claim_history)
    assert all("blocked:" not in entry[1] for entry in state.claim_history)


async def test_completed_no_pr_declared_releases_claim(tmp_path: Path) -> None:
    """`completed_no_pr_declared` → release_issue (operator-documented no-PR)."""
    orch, tracker = _make_orch(tmp_path, OUTCOME_COMPLETED_NO_PR_DECLARED)
    await orch.run_once()

    state = tracker.states["acme/proj#1"]
    assert state.blocked is False
    assert state.claimed_by is None


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
