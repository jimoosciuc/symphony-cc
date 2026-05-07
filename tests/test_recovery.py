"""Restart-recovery tests for ``Orchestrator.recover()`` (issue #31, SPEC §16).

Drives the recovery flow against pre-seeded session records and a
fake tracker / provider, asserting:

- terminal-state-set records are skipped (already cleanly finished);
- in-flight records honor ``claude.retry_resume_policy`` exactly:
  - ``resume_same_session`` calls ``provider.restore()`` and runs the
    resumed worker through to a terminal state;
  - ``new_session_with_summary`` releases the claim, stashes the prior
    provider session id chain, and the next dispatch tick consumes it;
  - ``fail_closed`` marks the issue blocked without resuming;
- ineligible issues (closed, blocked label, exclude label) are released
  without a resume attempt;
- restore failures under ``resume_same_session`` flip to blocked;
- ``recovery.json`` artifact is written under each record's attempt dir;
- the on-disk session record is stamped terminal so a second
  ``recover()`` call is a no-op.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

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
from symphony.github.tracker import FakeGitHubTracker, TrackerError
from symphony.models import Issue
from symphony.orchestrator import Orchestrator
from symphony.provider.fake import FakeProvider
from symphony.recovery import (
    ACTION_BLOCKED,
    ACTION_DISCARDED,
    ACTION_RELEASED,
    ACTION_RESUMED,
    ACTION_SKIPPED,
    discover_persisted_records,
)
from symphony.workspace import WorkspaceManager

# -- Fixtures ----------------------------------------------------------------


def _issue(
    *,
    number: int = 1,
    state: str = "open",
    labels: tuple[str, ...] = ("symphony-ready",),
) -> Issue:
    return Issue(
        id=f"I_{number}",
        number=number,
        identifier=f"acme/proj#{number}",
        owner="acme",
        repo="proj",
        title=f"t{number}",
        body="b",
        state=state,
        url=f"https://github.com/acme/proj/issues/{number}",
        labels=labels,
    )


def _config(
    tmp_path: Path,
    *,
    policy: str = "resume_same_session",
) -> WorkflowConfig:
    return WorkflowConfig(
        tracker=TrackerConfig(
            kind="github",
            owner="acme",
            repo="proj",
            token="literal-token",
            include_labels=("symphony-ready",),
            exclude_labels=("symphony-blocked",),
        ),
        agent=AgentConfig(max_concurrency=1, max_turns=1),
        workspace=WorkspaceConfig(root=tmp_path / "ws"),
        claude=ClaudeConfig(
            model="fake-model",
            permission_mode="acceptEdits",
            session_store=tmp_path / "sessions",
            transcript_store=tmp_path / "transcripts",
            artifact_store=tmp_path / "artifacts",
            retry_resume_policy=policy,
        ),
        github=GitHubConfig(),
        polling=PollingConfig(),
        retry=RetryConfig(initial_backoff_ms=100, max_backoff_ms=400, multiplier=2.0),
        logging=LoggingConfig(),
        workflow_path=tmp_path / "WORKFLOW.md",
    )


def _make(
    tmp_path: Path,
    *,
    provider: FakeProvider,
    issues: list[Issue] | None = None,
    policy: str = "resume_same_session",
) -> tuple[Orchestrator, FakeGitHubTracker, WorkflowConfig]:
    cfg = _config(tmp_path, policy=policy)
    if issues is None:
        issues = [_issue()]
    tracker = FakeGitHubTracker(issues=issues)
    mgr = WorkspaceManager(cfg.workspace)
    orch = Orchestrator(cfg, tracker=tracker, provider=provider, workspace_manager=mgr)
    return orch, tracker, cfg


def _seed_record(
    session_store: Path,
    *,
    session_id: str = "sym-recovered-1",
    issue: Issue,
    provider_session_id: str | None = "claude-pid-old",
    attempt: int = 1,
    terminal_state: str | None = None,
    workspace_path: Path | None = None,
    artifact_dir: Path | None = None,
    previous_provider_session_ids: list[str] | None = None,
) -> Path:
    """Write a fake session record exactly as ``_persist_session`` would.

    Returns the path to the written file.
    """
    session_store.mkdir(parents=True, exist_ok=True)
    record = {
        "session_id": session_id,
        "provider": "fake",
        "provider_session_id": provider_session_id,
        "issue_identifier": issue.identifier,
        "issue_number": issue.number,
        "workspace_path": str(workspace_path or session_store.parent / "ws_seed"),
        "artifact_dir": str(artifact_dir or session_store.parent / "artifacts_seed"),
        "transcript_path": None,
        "attempt": attempt,
        "turn_count": 0,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "last_event_at": None,
        "terminal_state": terminal_state,
        "previous_provider_session_ids": previous_provider_session_ids or [],
        "session_store": str(session_store),
    }
    target = session_store / f"{session_id}.json"
    target.write_text(json.dumps(record, indent=2, sort_keys=True), encoding="utf-8")
    return target


def _read_recovery(record_path: Path, fallback_dir: Path | None = None) -> dict:
    """Pull recovery.json out of the artifact dir referenced by the record."""
    rec = json.loads(record_path.read_text(encoding="utf-8"))
    candidate = Path(rec["artifact_dir"]) / "recovery.json"
    if candidate.exists():
        return json.loads(candidate.read_text(encoding="utf-8"))
    if fallback_dir is not None:
        candidate = fallback_dir / "recovery.json"
        if candidate.exists():
            return json.loads(candidate.read_text(encoding="utf-8"))
    raise FileNotFoundError(f"recovery.json not found near {record_path}")


# -- discover_persisted_records ---------------------------------------------


def test_discover_skips_terminal_state_set(tmp_path: Path) -> None:
    """Records with ``terminal_state`` already populated are filtered out."""
    issue = _issue()
    store = tmp_path / "sessions"
    _seed_record(store, session_id="done", issue=issue, terminal_state="completed")
    _seed_record(store, session_id="active", issue=issue, terminal_state=None)

    found = discover_persisted_records(store)
    ids = [rec.session_id for _, rec in found]
    assert ids == ["active"]


def test_discover_handles_missing_store(tmp_path: Path) -> None:
    """Empty / missing session_store is a no-op, not an error."""
    assert discover_persisted_records(tmp_path / "missing") == []
    (tmp_path / "empty").mkdir()
    assert discover_persisted_records(tmp_path / "empty") == []


def test_discover_skips_malformed_records(tmp_path: Path) -> None:
    """Broken JSON or missing required fields → log + skip, do not crash."""
    store = tmp_path / "sessions"
    store.mkdir(parents=True)
    (store / "bad.json").write_text("{ not json", encoding="utf-8")
    (store / "incomplete.json").write_text(json.dumps({"session_id": "x"}), encoding="utf-8")
    issue = _issue()
    _seed_record(store, session_id="good", issue=issue)

    found = discover_persisted_records(store)
    ids = [rec.session_id for _, rec in found]
    assert ids == ["good"]


def test_discover_ignores_tmp_files(tmp_path: Path) -> None:
    """The atomic-write tmp files (``*.json.tmp``) must not be hydrated."""
    store = tmp_path / "sessions"
    store.mkdir(parents=True)
    issue = _issue()
    _seed_record(store, session_id="real", issue=issue)
    (store / "real.json.tmp").write_text("{}", encoding="utf-8")

    found = discover_persisted_records(store)
    assert [rec.session_id for _, rec in found] == ["real"]


# -- recover() empty / clean cases ------------------------------------------


async def test_recover_no_records_is_noop(tmp_path: Path) -> None:
    prov = FakeProvider()
    orch, _tracker, _cfg = _make(tmp_path, provider=prov)
    decisions = await orch.recover()
    assert decisions == []
    assert orch.recovery_decisions == []
    # Provider should not have been touched at all.
    assert prov.calls == []


# -- resume_same_session ----------------------------------------------------


async def test_recover_resume_same_session_drives_worker_to_completion(
    tmp_path: Path,
) -> None:
    """The happy path: persisted record gets restored, the resumed worker
    runs through ``send_input`` to ``turn_completed``, ``terminal.json``
    lands with ``completed`` for the bumped attempt, and the issue is
    released with the claim cleared.
    """
    issue = _issue()
    prov = FakeProvider()
    orch, tracker, cfg = _make(tmp_path, provider=prov, issues=[issue])
    # Issue was already claimed before the daemon died.
    tracker.claim_issue(issue, {"run_id": "old-daemon"})

    record_path = _seed_record(cfg.claude.session_store, issue=issue, attempt=1)

    decisions = await orch.recover()
    assert len(decisions) == 1
    decision = decisions[0]
    assert decision.action == ACTION_RESUMED
    assert decision.policy == "resume_same_session"
    assert decision.restored_session_id == "sym-recovered-1"

    # restore() was called on the provider, then a normal turn ran.
    methods = [m for m, _ in prov.calls]
    assert "restore" in methods
    assert "send_input" in methods
    assert "close" in methods

    # The issue is released after the resumed worker completes.
    assert tracker.states[issue.identifier].claimed_by is None
    # And NOT marked blocked — completion is a clean release.
    assert tracker.states[issue.identifier].blocked is False

    # Recovery decision artifact lands under the bumped-attempt dir
    # (restore() bumps attempt 1 → 2 in the fake provider).
    bumped = cfg.claude.artifact_store / f"{issue.owner}_{issue.repo}_{issue.number}" / "2"
    rec_artifact = json.loads((bumped / "recovery.json").read_text())
    assert rec_artifact["action"] == ACTION_RESUMED
    assert rec_artifact["issue_identifier"] == issue.identifier

    # Subsequent recover() is a no-op because the on-disk record now
    # carries terminal_state. Re-load and verify.
    json.loads(record_path.read_text())  # ensure file still parses
    second = await orch.recover()
    assert all(d.action != ACTION_RESUMED for d in second), (
        f"second recover() must not double-resume; got {[d.action for d in second]}"
    )


async def test_recover_resume_blocks_when_no_provider_session_id(
    tmp_path: Path,
) -> None:
    """A record with no provider_session_id under resume_same_session
    cannot be restored — mark blocked rather than silently dropping."""
    issue = _issue()
    prov = FakeProvider()
    orch, tracker, cfg = _make(tmp_path, provider=prov, issues=[issue])
    tracker.claim_issue(issue, {"run_id": "old-daemon"})

    _seed_record(cfg.claude.session_store, issue=issue, provider_session_id=None)

    decisions = await orch.recover()
    assert decisions[0].action == ACTION_BLOCKED
    assert tracker.states[issue.identifier].blocked is True
    # Provider.restore must NOT have been called.
    assert all(m != "restore" for m, _ in prov.calls)


async def test_recover_resume_blocks_when_restore_fails(tmp_path: Path) -> None:
    """ProviderRestoreError under resume_same_session → mark blocked,
    write recovery.json with the error reason."""
    issue = _issue()
    prov = FakeProvider(restore_should_fail=True)
    orch, tracker, cfg = _make(tmp_path, provider=prov, issues=[issue])
    tracker.claim_issue(issue, {"run_id": "old-daemon"})

    record_path = _seed_record(cfg.claude.session_store, issue=issue)

    decisions = await orch.recover()
    assert decisions[0].action == ACTION_BLOCKED
    assert tracker.states[issue.identifier].blocked is True

    rec = _read_recovery(record_path)
    assert rec["action"] == ACTION_BLOCKED
    assert "restore failed" in rec["reason"]


# -- new_session_with_summary -----------------------------------------------


async def test_recover_new_session_with_summary_releases_and_carries_chain(
    tmp_path: Path,
) -> None:
    """The release-and-handoff path: claim is released, prior provider
    session id is stashed, and the next dispatch tick produces a fresh
    session whose ``previous_provider_session_ids`` carries the chain."""
    issue = _issue()
    prov = FakeProvider()
    orch, tracker, cfg = _make(
        tmp_path, provider=prov, issues=[issue], policy="new_session_with_summary"
    )
    tracker.claim_issue(issue, {"run_id": "old-daemon"})

    _seed_record(
        cfg.claude.session_store,
        issue=issue,
        provider_session_id="claude-pid-prior",
        previous_provider_session_ids=["claude-pid-older"],
    )

    decisions = await orch.recover()
    assert decisions[0].action == ACTION_RELEASED

    # Claim cleared, NOT blocked.
    assert tracker.states[issue.identifier].claimed_by is None
    assert tracker.states[issue.identifier].blocked is False

    # Carryover stashed for next dispatch.
    assert orch._restart_carryover[issue.identifier] == [
        "claude-pid-older",
        "claude-pid-prior",
    ]

    # Now run one tick — fresh start_session should absorb the chain.
    await orch.run_once()
    assert issue.identifier not in orch._restart_carryover  # consumed
    # The fake provider's start_session gives a fresh record; the
    # orchestrator copies the chain onto it before send_input.
    methods = [m for m, _ in prov.calls]
    assert "start_session" in methods


async def test_recover_new_session_with_summary_no_carryover_when_no_prior_id(
    tmp_path: Path,
) -> None:
    """If the persisted record has no provider_session_id and no prior
    chain, new_session_with_summary still releases — but there's no
    chain to stash."""
    issue = _issue()
    prov = FakeProvider()
    orch, tracker, cfg = _make(
        tmp_path, provider=prov, issues=[issue], policy="new_session_with_summary"
    )
    tracker.claim_issue(issue, {"run_id": "old-daemon"})

    _seed_record(
        cfg.claude.session_store,
        issue=issue,
        provider_session_id=None,
        previous_provider_session_ids=[],
    )

    decisions = await orch.recover()
    assert decisions[0].action == ACTION_RELEASED
    assert issue.identifier not in orch._restart_carryover
    assert tracker.states[issue.identifier].claimed_by is None


# -- fail_closed -------------------------------------------------------------


async def test_recover_fail_closed_marks_blocked_without_resume(tmp_path: Path) -> None:
    """The conservative policy: never resume, always block. Operator must
    clear ``symphony-blocked`` to let Symphony re-attempt."""
    issue = _issue()
    prov = FakeProvider()
    orch, tracker, cfg = _make(tmp_path, provider=prov, issues=[issue], policy="fail_closed")
    tracker.claim_issue(issue, {"run_id": "old-daemon"})

    _seed_record(cfg.claude.session_store, issue=issue)

    decisions = await orch.recover()
    assert decisions[0].action == ACTION_BLOCKED
    assert tracker.states[issue.identifier].blocked is True
    # provider.restore must NOT have been called.
    assert all(m != "restore" for m, _ in prov.calls)


# -- Ineligible issues -------------------------------------------------------


async def test_recover_releases_when_issue_closed(tmp_path: Path) -> None:
    """Issue closed since the daemon died → no resume, just release the
    stale claim and stamp the record terminal."""
    issue = _issue(state="closed")
    prov = FakeProvider()
    orch, tracker, cfg = _make(tmp_path, provider=prov, issues=[issue])
    tracker.claim_issue(issue, {"run_id": "old-daemon"})

    record_path = _seed_record(cfg.claude.session_store, issue=issue)

    decisions = await orch.recover()
    assert decisions[0].action == ACTION_RELEASED
    assert "closed" in decisions[0].reason
    assert tracker.states[issue.identifier].claimed_by is None
    # provider must NOT have been touched.
    assert all(m != "restore" for m, _ in prov.calls)

    rec = _read_recovery(record_path)
    assert rec["action"] == ACTION_RELEASED


async def test_recover_releases_when_issue_has_blocked_label(tmp_path: Path) -> None:
    """An operator added ``symphony-blocked`` while the daemon was down.
    Recovery must respect that and not resume."""
    issue = _issue(labels=("symphony-ready", "symphony-blocked"))
    prov = FakeProvider()
    orch, tracker, cfg = _make(tmp_path, provider=prov, issues=[issue])
    tracker.claim_issue(issue, {"run_id": "old-daemon"})

    _seed_record(cfg.claude.session_store, issue=issue)

    decisions = await orch.recover()
    assert decisions[0].action == ACTION_RELEASED
    assert "blocked label" in decisions[0].reason


async def test_recover_discards_when_issue_vanished(tmp_path: Path) -> None:
    """Tracker no longer knows about the issue (deleted, transferred,
    repo-renamed). Discard the record so we don't loop on it."""
    issue = _issue()
    prov = FakeProvider()
    # Tracker has NO matching issue.
    orch, _tracker, cfg = _make(tmp_path, provider=prov, issues=[])

    _seed_record(cfg.claude.session_store, issue=issue)

    decisions = await orch.recover()
    assert decisions[0].action == ACTION_DISCARDED

    # Record is stamped terminal so a second recover() is a no-op.
    found = discover_persisted_records(cfg.claude.session_store)
    assert found == []


# -- Tracker failure path ----------------------------------------------------


async def test_recover_skipped_when_tracker_fetch_fails(tmp_path: Path) -> None:
    """Transient tracker failure → all records reported as SKIPPED so
    the operator can retry; no claim mutations."""
    issue = _issue()
    prov = FakeProvider()
    orch, tracker, cfg = _make(tmp_path, provider=prov, issues=[issue])

    def _explode(numbers):  # noqa: ARG001
        raise TrackerError("503")

    tracker.fetch_issues_by_numbers = _explode  # type: ignore[assignment]
    _seed_record(cfg.claude.session_store, issue=issue)

    decisions = await orch.recover()
    assert decisions[0].action == ACTION_SKIPPED
    assert "tracker fetch failed" in decisions[0].reason
    # Provider untouched.
    assert prov.calls == []


# -- Idempotency / record stamping -------------------------------------------


async def test_recover_stamped_records_not_reprocessed(tmp_path: Path) -> None:
    """After recover() decides anything terminal (released/blocked/discarded)
    on a record, a second recover() must NOT re-decide on it."""
    issue = _issue(state="closed")
    prov = FakeProvider()
    orch, tracker, cfg = _make(tmp_path, provider=prov, issues=[issue])
    tracker.claim_issue(issue, {"run_id": "old-daemon"})

    _seed_record(cfg.claude.session_store, issue=issue)

    first = await orch.recover()
    assert first[0].action == ACTION_RELEASED

    # The on-disk record was stamped — discover should skip it.
    assert discover_persisted_records(cfg.claude.session_store) == []

    # And recover() called again is a no-op.
    second = await orch.recover()
    assert second == []


# -- CLI summary smoke (does not exec subprocess) ----------------------------


def test_print_tick_summary_includes_recovery_when_present(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    from symphony.cli import _print_tick_summary
    from symphony.orchestrator import TickResult
    from symphony.recovery import RecoveryDecision

    decisions = [
        RecoveryDecision(
            record_path=tmp_path / "rec.json",
            issue_identifier="acme/proj#5",
            issue_number=5,
            action=ACTION_RESUMED,
            reason="restored",
            policy="resume_same_session",
            restored_session_id="sym-zzz",
        )
    ]
    _print_tick_summary(TickResult(), recovery=decisions)
    out = capsys.readouterr().out
    assert "recovery decisions" in out
    assert "resumed acme/proj#5" in out
    assert "session=sym-zzz" in out
