"""Tests for the M5.7 workspace cleanup executor (issue #66).

Drives :class:`WorkspaceCleanupExecutor` against real on-disk
workspaces under a tmp_path root so the path-safety guards execute
the same code path production would. The executor is constructed
with a real :class:`WorkspaceManager` (its `delete()` already enforces
the outside-root / root-itself guards we delegate to).

Surface tested (mapping to #66 acceptance criteria):

- enabled=False is a no-op for all three triggers
- terminal-issue trigger deletes / dry_run / handles missing
- closed-PR trigger respects pr_state / on_closed_pr / dry_run
- age sweep deletes old workspaces, skips active identifiers, skips
  too-young workspaces
- path safety: deleting outside `workspace.root` is refused
- path safety: deleting `workspace.root` itself is refused
- idempotency: missing target paths return KEPT_NOT_FOUND, not raise
- key derivation `owner/repo#N` → `owner_repo_N` matches workspace
  manager's naming
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from symphony.cleanup import (
    CleanupAction,
    CleanupDecision,
    WorkspaceCleanupExecutor,
)
from symphony.config import WorkspaceCleanupConfig, WorkspaceConfig
from symphony.models import Issue, Workspace
from symphony.workspace import WorkspaceManager, workspace_key_from_identifier

# -- Fixtures ----------------------------------------------------------------


def _issue(*, owner: str = "acme", repo: str = "proj", number: int = 1) -> Issue:
    return Issue(
        id=f"I_{number}",
        number=number,
        identifier=f"{owner}/{repo}#{number}",
        owner=owner,
        repo=repo,
        title=f"t{number}",
        body="b",
        state="open",
        url=f"https://github.com/{owner}/{repo}/issues/{number}",
    )


def _make(
    tmp_path: Path,
    *,
    enabled: bool = False,
    on_terminal_issue: bool = False,
    on_closed_pr: bool = False,
    max_age_days: int | None = None,
    dry_run: bool = False,
    clock: Any = None,
) -> tuple[WorkspaceManager, WorkspaceCleanupExecutor]:
    cleanup = WorkspaceCleanupConfig(
        enabled=enabled,
        on_terminal_issue=on_terminal_issue,
        on_closed_pr=on_closed_pr,
        max_age_days=max_age_days,
        dry_run=dry_run,
    )
    cfg = WorkspaceConfig(root=tmp_path / "ws", cleanup=cleanup)
    mgr = WorkspaceManager(cfg)
    executor = WorkspaceCleanupExecutor(mgr, cleanup, clock=clock)
    return mgr, executor


def _prepare(mgr: WorkspaceManager, issue: Issue) -> Workspace:
    """Use the manager to prepare an actual workspace dir on disk."""
    return mgr.prepare(issue)


# -- enabled=False is a no-op -----------------------------------------------


def test_terminal_issue_noop_when_disabled(tmp_path: Path) -> None:
    mgr, ex = _make(tmp_path, enabled=False, on_terminal_issue=True)
    workspace = _prepare(mgr, _issue())
    decision = ex.cleanup_for_terminal_issue(workspace)
    assert decision.action == CleanupAction.KEPT_DISABLED
    assert workspace.path.exists()


def test_closed_pr_noop_when_disabled(tmp_path: Path) -> None:
    mgr, ex = _make(tmp_path, enabled=False, on_closed_pr=True)
    workspace = _prepare(mgr, _issue())
    decision = ex.cleanup_for_closed_pr(workspace, pr_state="merged")
    assert decision.action == CleanupAction.KEPT_DISABLED
    assert workspace.path.exists()


def test_age_sweep_noop_when_disabled(tmp_path: Path) -> None:
    mgr, ex = _make(tmp_path, enabled=False, max_age_days=1)
    _prepare(mgr, _issue())
    decisions = ex.sweep_for_age()
    assert decisions == []


# -- Trigger gating: enabled but trigger off → no-op ------------------------


def test_terminal_issue_noop_when_trigger_off(tmp_path: Path) -> None:
    mgr, ex = _make(tmp_path, enabled=True, on_terminal_issue=False, max_age_days=7)
    workspace = _prepare(mgr, _issue())
    decision = ex.cleanup_for_terminal_issue(workspace)
    assert decision.action == CleanupAction.KEPT_TRIGGER_NOT_SET
    assert workspace.path.exists()


def test_closed_pr_noop_when_trigger_off(tmp_path: Path) -> None:
    mgr, ex = _make(tmp_path, enabled=True, on_closed_pr=False, max_age_days=7)
    workspace = _prepare(mgr, _issue())
    decision = ex.cleanup_for_closed_pr(workspace, pr_state="merged")
    assert decision.action == CleanupAction.KEPT_TRIGGER_NOT_SET


# -- Terminal-issue trigger -------------------------------------------------


def test_terminal_issue_deletes_when_enabled(tmp_path: Path) -> None:
    mgr, ex = _make(tmp_path, enabled=True, on_terminal_issue=True)
    workspace = _prepare(mgr, _issue())
    assert workspace.path.exists()

    decision = ex.cleanup_for_terminal_issue(workspace)
    assert decision.action == CleanupAction.DELETED
    assert decision.trigger == "terminal_issue"
    assert not workspace.path.exists()


def test_terminal_issue_dry_run_does_not_delete(tmp_path: Path) -> None:
    mgr, ex = _make(tmp_path, enabled=True, on_terminal_issue=True, dry_run=True)
    workspace = _prepare(mgr, _issue())

    decision = ex.cleanup_for_terminal_issue(workspace)
    assert decision.action == CleanupAction.KEPT_DRY_RUN
    assert workspace.path.exists()


def test_terminal_issue_idempotent_on_missing(tmp_path: Path) -> None:
    """Workspace already deleted by an operator/prior tick: executor
    must NOT raise."""
    mgr, ex = _make(tmp_path, enabled=True, on_terminal_issue=True)
    workspace = _prepare(mgr, _issue())
    # Simulate operator manually removing the workspace.
    import shutil

    shutil.rmtree(workspace.path)

    decision = ex.cleanup_for_terminal_issue(workspace)
    assert decision.action == CleanupAction.KEPT_NOT_FOUND


# -- Closed-PR trigger ------------------------------------------------------


def test_closed_pr_deletes_for_merged(tmp_path: Path) -> None:
    mgr, ex = _make(tmp_path, enabled=True, on_closed_pr=True)
    workspace = _prepare(mgr, _issue())

    decision = ex.cleanup_for_closed_pr(workspace, pr_state="merged")
    assert decision.action == CleanupAction.DELETED
    assert decision.trigger == "closed_pr"
    assert not workspace.path.exists()


def test_closed_pr_deletes_for_closed(tmp_path: Path) -> None:
    """`closed` (without merge) is also a terminal PR state per
    GitHub semantics."""
    mgr, ex = _make(tmp_path, enabled=True, on_closed_pr=True)
    workspace = _prepare(mgr, _issue())

    decision = ex.cleanup_for_closed_pr(workspace, pr_state="closed")
    assert decision.action == CleanupAction.DELETED
    assert not workspace.path.exists()


def test_closed_pr_keeps_when_open(tmp_path: Path) -> None:
    """Open PRs MUST NOT trigger cleanup — Claude / operator may still
    push more commits to the linked branch."""
    mgr, ex = _make(tmp_path, enabled=True, on_closed_pr=True)
    workspace = _prepare(mgr, _issue())

    decision = ex.cleanup_for_closed_pr(workspace, pr_state="open")
    assert decision.action == CleanupAction.KEPT_PR_OPEN
    assert workspace.path.exists()


def test_closed_pr_dry_run_does_not_delete(tmp_path: Path) -> None:
    mgr, ex = _make(tmp_path, enabled=True, on_closed_pr=True, dry_run=True)
    workspace = _prepare(mgr, _issue())

    decision = ex.cleanup_for_closed_pr(workspace, pr_state="merged")
    assert decision.action == CleanupAction.KEPT_DRY_RUN
    assert workspace.path.exists()


# -- Age-based sweep --------------------------------------------------------


def test_age_sweep_deletes_old_workspaces(tmp_path: Path) -> None:
    """Workspaces with mtime older than cutoff are deleted."""
    # Set clock to a fixed "now"; backdate the workspace mtime.
    fixed_now = datetime(2026, 5, 8, 12, 0, 0, tzinfo=timezone.utc)
    mgr, ex = _make(
        tmp_path, enabled=True, max_age_days=7, clock=lambda: fixed_now
    )
    workspace = _prepare(mgr, _issue())
    # Backdate by 10 days.
    old_mtime = (fixed_now - timedelta(days=10)).timestamp()
    import os

    os.utime(workspace.path, (old_mtime, old_mtime))

    decisions = ex.sweep_for_age()
    assert len(decisions) == 1
    assert decisions[0].action == CleanupAction.DELETED
    assert decisions[0].trigger == "age"
    assert not workspace.path.exists()


def test_age_sweep_keeps_recent_workspaces(tmp_path: Path) -> None:
    fixed_now = datetime(2026, 5, 8, 12, 0, 0, tzinfo=timezone.utc)
    mgr, ex = _make(
        tmp_path, enabled=True, max_age_days=7, clock=lambda: fixed_now
    )
    workspace = _prepare(mgr, _issue())
    # Set mtime to 3 days ago (younger than the 7-day cutoff).
    young_mtime = (fixed_now - timedelta(days=3)).timestamp()
    import os

    os.utime(workspace.path, (young_mtime, young_mtime))

    decisions = ex.sweep_for_age()
    assert len(decisions) == 1
    assert decisions[0].action == CleanupAction.KEPT_TOO_YOUNG
    assert workspace.path.exists()


def test_age_sweep_skips_active_identifiers(tmp_path: Path) -> None:
    """A workspace held by an active worker MUST NEVER be deleted by
    the age sweep regardless of mtime."""
    fixed_now = datetime(2026, 5, 8, 12, 0, 0, tzinfo=timezone.utc)
    mgr, ex = _make(
        tmp_path, enabled=True, max_age_days=7, clock=lambda: fixed_now
    )
    issue = _issue(number=42)
    workspace = _prepare(mgr, issue)
    # Backdate aggressively.
    old_mtime = (fixed_now - timedelta(days=30)).timestamp()
    import os

    os.utime(workspace.path, (old_mtime, old_mtime))

    decisions = ex.sweep_for_age(active_identifiers={issue.identifier})
    assert len(decisions) == 1
    assert decisions[0].action == CleanupAction.KEPT_ACTIVE
    assert workspace.path.exists()  # critical safety property


def test_age_sweep_no_op_when_max_age_unset(tmp_path: Path) -> None:
    mgr, ex = _make(tmp_path, enabled=True, max_age_days=None)
    _prepare(mgr, _issue())
    decisions = ex.sweep_for_age()
    assert decisions == []


def test_age_sweep_handles_empty_root(tmp_path: Path) -> None:
    """Sweep on a brand-new workspace.root (no children) returns []."""
    mgr, ex = _make(tmp_path, enabled=True, max_age_days=7)
    # Root doesn't exist yet — should not raise.
    decisions = ex.sweep_for_age()
    assert decisions == []


def test_age_sweep_iterates_multiple_workspaces(tmp_path: Path) -> None:
    fixed_now = datetime(2026, 5, 8, 12, 0, 0, tzinfo=timezone.utc)
    mgr, ex = _make(
        tmp_path, enabled=True, max_age_days=7, clock=lambda: fixed_now
    )
    old_issue = _issue(number=1)
    young_issue = _issue(number=2)
    active_issue = _issue(number=3)
    old_ws = _prepare(mgr, old_issue)
    young_ws = _prepare(mgr, young_issue)
    active_ws = _prepare(mgr, active_issue)

    import os

    os.utime(old_ws.path, ((fixed_now - timedelta(days=10)).timestamp(),) * 2)
    os.utime(young_ws.path, ((fixed_now - timedelta(days=3)).timestamp(),) * 2)
    os.utime(active_ws.path, ((fixed_now - timedelta(days=20)).timestamp(),) * 2)

    decisions = ex.sweep_for_age(active_identifiers={active_issue.identifier})
    by_path = {d.workspace_path.name: d for d in decisions}
    assert by_path[old_ws.path.name].action == CleanupAction.DELETED
    assert by_path[young_ws.path.name].action == CleanupAction.KEPT_TOO_YOUNG
    assert by_path[active_ws.path.name].action == CleanupAction.KEPT_ACTIVE


# -- Path safety guards -----------------------------------------------------


def test_delete_outside_root_is_refused(tmp_path: Path) -> None:
    """A workspace whose path resolves outside `workspace.root` MUST
    NOT be deleted, even when all triggers are set. Defense against a
    future caller passing a hand-built Workspace dataclass."""
    mgr, ex = _make(tmp_path, enabled=True, on_terminal_issue=True)
    # Build a Workspace pointing somewhere completely unrelated.
    rogue = tmp_path / "OUTSIDE_ROOT"
    rogue.mkdir()
    workspace = Workspace(
        issue_identifier="acme/proj#1",
        workspace_key="rogue",
        path=rogue,
        repo_path=rogue,
        created_at=datetime.now(timezone.utc),
        reused=False,
    )

    decision = ex.cleanup_for_terminal_issue(workspace)
    assert decision.action == CleanupAction.KEPT_OUTSIDE_ROOT
    assert rogue.exists()


def test_delete_workspace_root_itself_is_refused(tmp_path: Path) -> None:
    """The configured `workspace.root` MUST NEVER be deleted — even
    from within the executor."""
    mgr, ex = _make(tmp_path, enabled=True, on_terminal_issue=True)
    # Ensure the root exists.
    mgr.root.mkdir(parents=True, exist_ok=True)
    workspace = Workspace(
        issue_identifier="acme/proj#1",
        workspace_key=mgr.root.name,
        path=mgr.root,
        repo_path=mgr.root,
        created_at=datetime.now(timezone.utc),
        reused=False,
    )

    decision = ex.cleanup_for_terminal_issue(workspace)
    assert decision.action == CleanupAction.KEPT_OUTSIDE_ROOT
    assert mgr.root.exists()


# -- Workspace key derivation ----------------------------------------------


def test_workspace_key_from_identifier_matches_manager_naming(tmp_path: Path) -> None:
    """`owner/repo#N` → `owner_repo_N` MUST match the key
    `WorkspaceManager.workspace_path` produces. Otherwise the age
    sweep's `active_identifiers` filter would fail to match."""
    mgr, _ = _make(tmp_path)
    issue = _issue(owner="acme", repo="proj", number=42)
    expected_key = mgr.workspace_path(issue).name
    assert workspace_key_from_identifier(issue.identifier) == expected_key


def test_workspace_key_from_identifier_handles_missing_hash() -> None:
    """Defensive: if a caller passes `owner/repo` without `#N`, the
    derived key should fall back gracefully (no crash)."""
    assert workspace_key_from_identifier("owner/repo") == "owner_repo"


# -- CleanupDecision shape --------------------------------------------------


def test_cleanup_decision_is_immutable_and_carries_metadata(tmp_path: Path) -> None:
    mgr, ex = _make(tmp_path, enabled=True, on_terminal_issue=True)
    workspace = _prepare(mgr, _issue())

    decision = ex.cleanup_for_terminal_issue(workspace)
    assert isinstance(decision, CleanupDecision)
    assert decision.trigger == "terminal_issue"
    assert decision.workspace_path == workspace.path.resolve()
    # frozen dataclass: assignment must fail.
    with pytest.raises(dataclasses.FrozenInstanceError):
        decision.action = CleanupAction.KEPT_DISABLED  # type: ignore[misc]


# -- Logging assertions ----------------------------------------------------


def test_dry_run_emits_warning_log(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Operators running with `--log-level info` MUST see dry_run
    decisions in the WARNING stream so a misconfigured dry_run is
    visible without parsing per-tick artifacts."""
    import logging as _logging

    mgr, ex = _make(tmp_path, enabled=True, on_terminal_issue=True, dry_run=True)
    workspace = _prepare(mgr, _issue())

    with caplog.at_level(_logging.WARNING, logger="symphony.cleanup"):
        ex.cleanup_for_terminal_issue(workspace)
    assert any(
        "dry_run" in r.getMessage() and str(workspace.path) in r.getMessage()
        for r in caplog.records
    )


def test_actual_delete_emits_info_log(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    import logging as _logging

    mgr, ex = _make(tmp_path, enabled=True, on_terminal_issue=True)
    workspace = _prepare(mgr, _issue())

    with caplog.at_level(_logging.INFO, logger="symphony.cleanup"):
        ex.cleanup_for_terminal_issue(workspace)
    assert any(
        "cleanup deleted workspace" in r.getMessage() for r in caplog.records
    )


# -- Orchestrator wiring smoke ---------------------------------------------


async def test_orchestrator_terminal_cleanup_fires_on_completion(
    tmp_path: Path,
) -> None:
    """End-to-end smoke: when workspace.cleanup is enabled with
    on_terminal_issue=True AND the worker reaches a clean task
    outcome, the workspace is deleted in the worker finally block.

    Uses a stub detector forcing `completed_with_pr` so the routing
    falls into the cleanup path."""
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
        OUTCOME_COMPLETED_WITH_PR,
        DetectorResult,
    )
    from symphony.github.tracker import FakeGitHubTracker
    from symphony.orchestrator import Orchestrator
    from symphony.provider.fake import FakeProvider

    cfg = WorkflowConfig(
        tracker=TrackerConfig(
            kind="github",
            owner="acme",
            repo="proj",
            token="literal-token",
            include_labels=("symphony-ready",),
        ),
        agent=AgentConfig(max_concurrency=1, max_turns=1),
        workspace=WorkspaceConfig(
            root=tmp_path / "ws",
            cleanup=WorkspaceCleanupConfig(enabled=True, on_terminal_issue=True),
        ),
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
    tracker = FakeGitHubTracker(
        issues=[
            Issue(
                id="I_1",
                number=1,
                identifier="acme/proj#1",
                owner="acme",
                repo="proj",
                title="t",
                body="b",
                state="open",
                url="https://github.com/acme/proj/issues/1",
                labels=("symphony-ready",),
            )
        ]
    )
    mgr = WorkspaceManager(cfg.workspace)

    class _StubDetector:
        def detect(self, **kwargs):
            return DetectorResult(
                task_outcome=OUTCOME_COMPLETED_WITH_PR,
                task_evidence=[],
                outcome_decided_by=DECIDED_BY_DETECTOR,
            )

    orch = Orchestrator(
        cfg,
        tracker=tracker,
        provider=FakeProvider(),
        workspace_manager=mgr,
        evidence_detector=_StubDetector(),
    )
    await orch.run_once()

    # Workspace was deleted because cleanup was wired with
    # on_terminal_issue=True and detector returned completed_with_pr.
    expected_path = tmp_path / "ws" / "acme_proj_1"
    assert not expected_path.exists()


async def test_orchestrator_terminal_cleanup_skipped_for_incomplete_outcome(
    tmp_path: Path,
) -> None:
    """An incomplete_no_evidence run is routed to mark_issue_blocked
    AND its workspace is preserved — the operator needs to inspect it.
    Even with on_terminal_issue=True, cleanup MUST NOT fire on
    incomplete outcomes."""
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
        OUTCOME_INCOMPLETE_NO_EVIDENCE,
        DetectorResult,
    )
    from symphony.github.tracker import FakeGitHubTracker
    from symphony.orchestrator import Orchestrator
    from symphony.provider.fake import FakeProvider

    cfg = WorkflowConfig(
        tracker=TrackerConfig(
            kind="github",
            owner="acme",
            repo="proj",
            token="literal-token",
            include_labels=("symphony-ready",),
        ),
        agent=AgentConfig(max_concurrency=1, max_turns=1),
        workspace=WorkspaceConfig(
            root=tmp_path / "ws",
            cleanup=WorkspaceCleanupConfig(enabled=True, on_terminal_issue=True),
        ),
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
    tracker = FakeGitHubTracker(
        issues=[
            Issue(
                id="I_1",
                number=1,
                identifier="acme/proj#1",
                owner="acme",
                repo="proj",
                title="t",
                body="b",
                state="open",
                url="https://github.com/acme/proj/issues/1",
                labels=("symphony-ready",),
            )
        ]
    )
    mgr = WorkspaceManager(cfg.workspace)

    class _StubDetector:
        def detect(self, **kwargs):
            return DetectorResult(
                task_outcome=OUTCOME_INCOMPLETE_NO_EVIDENCE,
                task_evidence=[],
                outcome_decided_by=DECIDED_BY_DETECTOR,
            )

    orch = Orchestrator(
        cfg,
        tracker=tracker,
        provider=FakeProvider(),
        workspace_manager=mgr,
        evidence_detector=_StubDetector(),
    )
    await orch.run_once()

    # CRITICAL: workspace preserved so operator can triage the
    # misleading-success run.
    expected_path = tmp_path / "ws" / "acme_proj_1"
    assert expected_path.exists()


# -- Closed-PR sweep wiring (#82) -------------------------------------------


def _closed_pr_cfg(tmp_path: Path, **cleanup_overrides: Any) -> Any:
    """Build a minimal WorkflowConfig for closed-PR sweep tests."""
    from symphony.config import (
        AgentConfig,
        ClaudeConfig,
        GitHubConfig,
        LoggingConfig,
        PollingConfig,
        RetryConfig,
        TrackerConfig,
        WorkflowConfig,
    )

    cleanup = WorkspaceCleanupConfig(
        enabled=cleanup_overrides.get("enabled", True),
        on_terminal_issue=cleanup_overrides.get("on_terminal_issue", False),
        on_closed_pr=cleanup_overrides.get("on_closed_pr", True),
        max_age_days=cleanup_overrides.get("max_age_days"),
        dry_run=cleanup_overrides.get("dry_run", False),
    )
    return WorkflowConfig(
        tracker=TrackerConfig(
            kind="github",
            owner="acme",
            repo="proj",
            token="literal-token",
            include_labels=("symphony-ready",),
        ),
        agent=AgentConfig(max_concurrency=1, max_turns=1),
        workspace=WorkspaceConfig(root=tmp_path / "ws", cleanup=cleanup),
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


def _make_orchestrator_for_sweep(
    tmp_path: Path,
    *,
    cleanup_overrides: dict | None = None,
):
    from symphony.github.tracker import FakeGitHubTracker
    from symphony.orchestrator import Orchestrator
    from symphony.provider.fake import FakeProvider

    cfg = _closed_pr_cfg(tmp_path, **(cleanup_overrides or {}))
    tracker = FakeGitHubTracker(issues=[])
    mgr = WorkspaceManager(cfg.workspace)
    orch = Orchestrator(
        cfg,
        tracker=tracker,
        provider=FakeProvider(),
        workspace_manager=mgr,
    )
    return orch, tracker, mgr


def _seed_workspace_dir(mgr: WorkspaceManager, number: int) -> Path:
    """Create an on-disk workspace dir mirroring WorkspaceManager naming."""
    path = mgr.root / f"acme_proj_{number}"
    path.mkdir(parents=True, exist_ok=True)
    (path / "marker").write_text("seed")
    return path


def _pr(number: int, *, state: str) -> Any:
    from symphony.models import PullRequest

    return PullRequest(
        id=f"PR_{number}",
        number=number,
        owner="acme",
        repo="proj",
        title=f"PR{number}",
        url=f"https://github.com/acme/proj/pull/{number}",
        state=state,
        head_ref=f"feat/{number}",
        base_ref="main",
    )


@pytest.mark.asyncio
async def test_orchestrator_closed_pr_sweep_deletes_when_pr_merged(
    tmp_path: Path,
) -> None:
    """End-to-end: a workspace with a merged linked PR is deleted on
    the next ``run_once`` tick when on_closed_pr=True."""
    orch, tracker, mgr = _make_orchestrator_for_sweep(tmp_path)
    path = _seed_workspace_dir(mgr, 7)
    # Add the issue to the tracker (it must exist so find_linked_pull_requests
    # can return PRs against it).
    tracker.add_issue(
        Issue(
            id="I_7",
            number=7,
            identifier="acme/proj#7",
            owner="acme",
            repo="proj",
            title="t",
            body="b",
            state="closed",
            url="https://github.com/acme/proj/issues/7",
        )
    )
    tracker.add_linked_pr("acme/proj#7", _pr(700, state="merged"))

    await orch.run_once()

    assert not path.exists()


@pytest.mark.asyncio
async def test_orchestrator_closed_pr_sweep_deletes_when_pr_closed(
    tmp_path: Path,
) -> None:
    """A PR in ``closed`` state (not merged) also triggers cleanup."""
    orch, tracker, mgr = _make_orchestrator_for_sweep(tmp_path)
    path = _seed_workspace_dir(mgr, 8)
    tracker.add_issue(
        Issue(
            id="I_8",
            number=8,
            identifier="acme/proj#8",
            owner="acme",
            repo="proj",
            title="t",
            body="b",
            state="closed",
            url="https://github.com/acme/proj/issues/8",
        )
    )
    tracker.add_linked_pr("acme/proj#8", _pr(800, state="closed"))

    await orch.run_once()

    assert not path.exists()


@pytest.mark.asyncio
async def test_orchestrator_closed_pr_sweep_preserves_open_pr(
    tmp_path: Path,
) -> None:
    """An open linked PR preserves the workspace — operator may still
    need to push commits to that PR."""
    orch, tracker, mgr = _make_orchestrator_for_sweep(tmp_path)
    path = _seed_workspace_dir(mgr, 9)
    tracker.add_issue(
        Issue(
            id="I_9",
            number=9,
            identifier="acme/proj#9",
            owner="acme",
            repo="proj",
            title="t",
            body="b",
            state="open",
            url="https://github.com/acme/proj/issues/9",
        )
    )
    tracker.add_linked_pr("acme/proj#9", _pr(900, state="open"))

    await orch.run_once()

    assert path.exists()


@pytest.mark.asyncio
async def test_orchestrator_closed_pr_sweep_preserves_when_any_pr_open(
    tmp_path: Path,
) -> None:
    """If multiple PRs are linked and ANY is still open, preserve."""
    orch, tracker, mgr = _make_orchestrator_for_sweep(tmp_path)
    path = _seed_workspace_dir(mgr, 10)
    tracker.add_issue(
        Issue(
            id="I_10",
            number=10,
            identifier="acme/proj#10",
            owner="acme",
            repo="proj",
            title="t",
            body="b",
            state="open",
            url="https://github.com/acme/proj/issues/10",
        )
    )
    tracker.add_linked_pr("acme/proj#10", _pr(1000, state="merged"))
    tracker.add_linked_pr("acme/proj#10", _pr(1001, state="open"))

    await orch.run_once()

    assert path.exists()


@pytest.mark.asyncio
async def test_orchestrator_closed_pr_sweep_skips_active_workspace(
    tmp_path: Path,
) -> None:
    """An active worker's workspace must NEVER be deleted regardless
    of PR state."""
    orch, tracker, mgr = _make_orchestrator_for_sweep(tmp_path)
    path = _seed_workspace_dir(mgr, 11)
    issue = Issue(
        id="I_11",
        number=11,
        identifier="acme/proj#11",
        owner="acme",
        repo="proj",
        title="t",
        body="b",
        state="open",
        url="https://github.com/acme/proj/issues/11",
    )
    tracker.add_issue(issue)
    tracker.add_linked_pr("acme/proj#11", _pr(1100, state="merged"))
    # The sweep only reads ``self.active.keys()`` — stub a sentinel
    # value to register the identifier as active without standing up
    # the full WorkerState plumbing (session/artifacts/etc.).
    orch.active[issue.identifier] = object()  # type: ignore[assignment]

    orch._sweep_for_closed_prs()

    assert path.exists()


def test_orchestrator_closed_pr_sweep_tolerates_tracker_error(
    tmp_path: Path,
) -> None:
    """One bad PR lookup must not abort the sweep — other workspaces
    should still be evaluated."""
    from symphony.github.tracker import TrackerError

    orch, tracker, mgr = _make_orchestrator_for_sweep(tmp_path)
    bad_path = _seed_workspace_dir(mgr, 20)
    good_path = _seed_workspace_dir(mgr, 21)

    tracker.add_issue(
        Issue(
            id="I_20",
            number=20,
            identifier="acme/proj#20",
            owner="acme",
            repo="proj",
            title="t",
            body="b",
            state="open",
            url="https://github.com/acme/proj/issues/20",
        )
    )
    tracker.add_issue(
        Issue(
            id="I_21",
            number=21,
            identifier="acme/proj#21",
            owner="acme",
            repo="proj",
            title="t",
            body="b",
            state="closed",
            url="https://github.com/acme/proj/issues/21",
        )
    )
    tracker.add_linked_pr("acme/proj#21", _pr(2100, state="merged"))

    original = tracker.find_linked_pull_requests

    def _selective_fail(issue: Issue):
        if issue.number == 20:
            raise TrackerError("simulated lookup failure")
        return original(issue)

    tracker.find_linked_pull_requests = _selective_fail  # type: ignore[method-assign]

    orch._sweep_for_closed_prs()

    # Bad workspace preserved (couldn't verify); good workspace deleted.
    assert bad_path.exists()
    assert not good_path.exists()


def test_orchestrator_closed_pr_sweep_noop_when_disabled(
    tmp_path: Path,
) -> None:
    """No iteration when workspace.cleanup.enabled=False."""
    orch, tracker, mgr = _make_orchestrator_for_sweep(
        tmp_path, cleanup_overrides={"enabled": False, "on_closed_pr": True}
    )
    path = _seed_workspace_dir(mgr, 30)
    tracker.add_issue(
        Issue(
            id="I_30",
            number=30,
            identifier="acme/proj#30",
            owner="acme",
            repo="proj",
            title="t",
            body="b",
            state="closed",
            url="https://github.com/acme/proj/issues/30",
        )
    )
    tracker.add_linked_pr("acme/proj#30", _pr(3000, state="merged"))

    orch._sweep_for_closed_prs()

    assert path.exists()


def test_orchestrator_closed_pr_sweep_noop_when_trigger_off(
    tmp_path: Path,
) -> None:
    """No iteration when on_closed_pr=False even if enabled=True."""
    orch, tracker, mgr = _make_orchestrator_for_sweep(
        tmp_path,
        cleanup_overrides={
            "enabled": True,
            "on_closed_pr": False,
            "on_terminal_issue": True,  # need at least one trigger to validate
        },
    )
    path = _seed_workspace_dir(mgr, 31)
    tracker.add_issue(
        Issue(
            id="I_31",
            number=31,
            identifier="acme/proj#31",
            owner="acme",
            repo="proj",
            title="t",
            body="b",
            state="closed",
            url="https://github.com/acme/proj/issues/31",
        )
    )
    tracker.add_linked_pr("acme/proj#31", _pr(3100, state="merged"))

    orch._sweep_for_closed_prs()

    assert path.exists()


def test_orchestrator_closed_pr_sweep_dry_run_preserves_disk(
    tmp_path: Path,
) -> None:
    """``dry_run=True`` reports the would-delete decision but does NOT
    touch disk."""
    orch, tracker, mgr = _make_orchestrator_for_sweep(
        tmp_path, cleanup_overrides={"dry_run": True}
    )
    path = _seed_workspace_dir(mgr, 40)
    tracker.add_issue(
        Issue(
            id="I_40",
            number=40,
            identifier="acme/proj#40",
            owner="acme",
            repo="proj",
            title="t",
            body="b",
            state="closed",
            url="https://github.com/acme/proj/issues/40",
        )
    )
    tracker.add_linked_pr("acme/proj#40", _pr(4000, state="merged"))

    orch._sweep_for_closed_prs()

    assert path.exists()


def test_orchestrator_closed_pr_sweep_skips_dirs_outside_naming_convention(
    tmp_path: Path,
) -> None:
    """A foreign dir under workspace.root that doesn't match the
    ``{owner}_{repo}_{number}`` prefix is left alone, even if a
    similarly-numbered PR is merged."""
    orch, _tracker, mgr = _make_orchestrator_for_sweep(tmp_path)
    foreign = mgr.root / "scratchpad-not-a-workspace"
    foreign.mkdir(parents=True)
    (foreign / "marker").write_text("seed")

    orch._sweep_for_closed_prs()

    assert foreign.exists()


def test_orchestrator_closed_pr_sweep_skips_when_no_linked_prs(
    tmp_path: Path,
) -> None:
    """An empty linked-PR list preserves the workspace — no terminal
    evidence to act on."""
    orch, tracker, mgr = _make_orchestrator_for_sweep(tmp_path)
    path = _seed_workspace_dir(mgr, 50)
    tracker.add_issue(
        Issue(
            id="I_50",
            number=50,
            identifier="acme/proj#50",
            owner="acme",
            repo="proj",
            title="t",
            body="b",
            state="open",
            url="https://github.com/acme/proj/issues/50",
        )
    )
    # No linked PRs added.

    orch._sweep_for_closed_prs()

    assert path.exists()
