"""Cancellation, timeout, crash, and cleanup tests (issue #10).

Drives the orchestrator through every terminal-state path and asserts:

- the right provider method is called (interrupt / cancel / close);
- ``terminal.json`` carries ``reason`` + ``retryable`` + ``subtype``;
- non-retryable failures call ``mark_issue_blocked`` instead of
  ``release_issue``;
- timeouts surface as retryable cancellations;
- workspace ``after_run`` runs in the finally block of every path.

Uses real (small) timeout values plus the existing scripted FakeProvider.
"""

from __future__ import annotations

import asyncio
import json
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
from symphony.events import AgentEvent
from symphony.github.tracker import FakeGitHubTracker
from symphony.models import Issue
from symphony.orchestrator import Orchestrator, WorkerState
from symphony.provider.base import ProviderError, ProviderRetryableError
from symphony.provider.fake import FakeProvider, FakeTurnScript
from symphony.workspace import WorkspaceManager

# -- Fixtures ----------------------------------------------------------------


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


def _config(
    tmp_path: Path,
    *,
    stall_ms: int = 200,
    turn_ms: int = 1000,
    max_turns: int = 1,
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
        agent=AgentConfig(max_concurrency=1, max_turns=max_turns),
        workspace=WorkspaceConfig(root=tmp_path / "ws"),
        claude=ClaudeConfig(
            model="fake-model",
            permission_mode="acceptEdits",
            session_store=tmp_path / "sessions",
            transcript_store=tmp_path / "transcripts",
            artifact_store=tmp_path / "artifacts",
            stall_timeout_ms=stall_ms,
            turn_timeout_ms=turn_ms,
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
    stall_ms: int = 200,
    turn_ms: int = 1000,
    max_turns: int = 1,
) -> tuple[Orchestrator, FakeGitHubTracker]:
    cfg = _config(tmp_path, stall_ms=stall_ms, turn_ms=turn_ms, max_turns=max_turns)
    tracker = FakeGitHubTracker(issues=issues or [_issue()])
    mgr = WorkspaceManager(cfg.workspace)
    orch = Orchestrator(cfg, tracker=tracker, provider=provider, workspace_manager=mgr)
    return orch, tracker


def _read_terminal(tmp_path: Path) -> dict:
    """Read the single terminal.json under tmp_path/artifacts."""
    path = next(Path(tmp_path / "artifacts").rglob("terminal.json"))
    return json.loads(path.read_text())


# -- A custom fake helper for timing-driven tests ----------------------------


class _StallProvider(FakeProvider):
    """FakeProvider whose first send_input sleeps long enough to trigger
    the stall timeout. Subsequent calls behave normally."""

    name = "fake"

    def __init__(self, sleep_seconds: float) -> None:
        super().__init__()
        self._sleep_seconds = sleep_seconds
        self._first = True

    async def send_input(self, session, message):  # type: ignore[override]
        # Synthesize the session_started shape from the parent so the
        # orchestrator's first-event handling works, then sleep without
        # yielding anything else — the orchestrator's stall timer trips.
        if self._first:
            self._first = False
            session.provider_session_id = self._allocate_provider_session_id()
            session.turn_count += 1
            self.calls.append(("send_input", session.session_id))
            from datetime import datetime, timezone

            from symphony.events import AgentEvent

            yield AgentEvent(
                event="session_started",
                timestamp=datetime.now(timezone.utc),
                session_id=session.session_id,
                provider=self.name,
                issue_identifier=session.issue_identifier,
                attempt=session.attempt,
                payload={"model": "fake", "session_id": session.provider_session_id},
                provider_session_id=session.provider_session_id,
            )
            await asyncio.sleep(self._sleep_seconds)
            # If we get past the sleep without being cancelled, yield a
            # terminal so the test doesn't hang.
            yield AgentEvent(
                event="turn_completed",
                timestamp=datetime.now(timezone.utc),
                session_id=session.session_id,
                provider=self.name,
                issue_identifier=session.issue_identifier,
                attempt=session.attempt,
                payload={"duration_ms": 0},
                provider_session_id=session.provider_session_id,
            )
        else:
            # Fall back to scripted behavior.
            async for ev in super().send_input(session, message):
                yield ev


# -- Stall timeout -----------------------------------------------------------


async def test_stall_timeout_interrupts_and_marks_retryable(tmp_path: Path) -> None:
    prov = _StallProvider(sleep_seconds=0.5)
    orch, tracker = _make(tmp_path, provider=prov, stall_ms=80, turn_ms=2000)
    result = await orch.run_once()

    assert result.dispatched == ["acme/proj#1"]
    assert "acme/proj#1" in result.retries_scheduled

    methods = [m for m, _ in prov.calls]
    assert "send_input" in methods
    assert "interrupt" in methods, "stall timeout must call provider.interrupt"
    assert "close" in methods

    rs = orch.retry_states["acme/proj#1"]
    assert rs.next_attempt_at is not None  # retryable

    record = _read_terminal(tmp_path)
    assert record["terminal_state"] == "cancelled"
    assert record["subtype"] == "stall_timeout"
    assert record["reason"] == "stall_timeout"
    assert record["retryable"] is True
    assert record["blocked"] is False
    assert tracker.states["acme/proj#1"].claimed_by is None


# -- Turn timeout ------------------------------------------------------------


async def test_turn_timeout_interrupts_and_marks_retryable(tmp_path: Path) -> None:
    """Turn timeout fires when total wallclock exceeds turn_timeout_ms even
    if the provider keeps emitting events fast enough to avoid stall.
    Scripted approach: the provider sleeps just under the stall budget but
    longer than the turn budget.
    """
    prov = _StallProvider(sleep_seconds=0.4)
    orch, tracker = _make(tmp_path, provider=prov, stall_ms=2000, turn_ms=80)
    result = await orch.run_once()

    assert "acme/proj#1" in result.retries_scheduled
    record = _read_terminal(tmp_path)
    assert record["subtype"] == "turn_timeout"
    assert record["reason"] == "turn_timeout"
    assert record["retryable"] is True


# -- Explicit cancellation (reconcile) ---------------------------------------


async def test_explicit_cancel_via_reconcile(tmp_path: Path) -> None:
    """When an issue becomes ineligible mid-flight (e.g. closed), the
    orchestrator should call provider.cancel and release the claim."""
    prov = FakeProvider()
    orch, tracker = _make(tmp_path, provider=prov, issues=[])

    issue = _issue(number=99)
    tracker.add_issue(issue)
    workspace = orch.workspaces.prepare(issue)
    session = await prov.start_session(issue, workspace.path, orch.config.claude)
    from symphony.artifacts import ArtifactWriter

    artifacts = ArtifactWriter.for_attempt(
        orch.config.claude.artifact_store,
        owner=issue.owner,
        repo=issue.repo,
        issue_number=issue.number,
        attempt=1,
        redact_keys=orch.config.logging.redact_keys,
    )
    orch.active[issue.identifier] = WorkerState(
        issue=issue, workspace=workspace, session=session, artifacts=artifacts
    )

    tracker.set_issue_state(issue.identifier, "closed")
    result = await orch.run_once()

    assert issue.identifier in result.reconciled_cancelled
    methods = [m for m, _ in prov.calls]
    assert "cancel" in methods
    assert tracker.states[issue.identifier].claimed_by is None


# -- Provider crash (non-zero exit / exception) ------------------------------


async def test_provider_crash_via_exception_marks_blocked_when_non_retryable(
    tmp_path: Path,
) -> None:
    """A bare ProviderError raised mid-stream is non-retryable. The
    orchestrator should call mark_issue_blocked and the artifact should
    record blocked=True with retryable=False.
    """
    bad = FakeTurnScript(events=[], raise_after=0, raise_with=ProviderError)
    prov = FakeProvider(default_script=bad)
    orch, tracker = _make(tmp_path, provider=prov)
    await orch.run_once()

    state = tracker.states["acme/proj#1"]
    assert state.blocked is True
    assert state.claimed_by is None  # blocked also drops claim

    record = _read_terminal(tmp_path)
    assert record["terminal_state"] == "failed"
    assert record["retryable"] is False
    assert record["blocked"] is True


async def test_provider_retryable_crash_releases_and_schedules_retry(
    tmp_path: Path,
) -> None:
    bad = FakeTurnScript(events=[], raise_after=0, raise_with=ProviderRetryableError)
    prov = FakeProvider(default_script=bad)
    orch, tracker = _make(tmp_path, provider=prov)
    result = await orch.run_once()

    assert "acme/proj#1" in result.retries_scheduled
    state = tracker.states["acme/proj#1"]
    assert state.blocked is False
    assert state.claimed_by is None  # released, not blocked

    record = _read_terminal(tmp_path)
    assert record["retryable"] is True
    assert record["blocked"] is False


# -- Stream ends without terminal (already covered in #18; re-asserts here
#    for the terminal.json shape) ------------------------------------------


async def test_no_terminal_event_marks_crashed_and_retryable(tmp_path: Path) -> None:
    truncated = FakeTurnScript(events=[("message_delta", {"text": "partial"})])
    prov = FakeProvider(default_script=truncated)
    orch, _ = _make(tmp_path, provider=prov)
    result = await orch.run_once()

    assert "acme/proj#1" in result.retries_scheduled
    record = _read_terminal(tmp_path)
    assert record["terminal_state"] == "crashed"
    assert record["retryable"] is True
    assert record["blocked"] is False


# -- Successful path: terminal.json shape -----------------------------------


async def test_successful_run_terminal_json_shape(tmp_path: Path) -> None:
    orch, _ = _make(tmp_path, provider=FakeProvider())
    await orch.run_once()
    record = _read_terminal(tmp_path)
    assert record["terminal_state"] == "completed"
    assert record["reason"] == "completed"
    assert record["retryable"] is False
    assert record["blocked"] is False
    assert record["subtype"] is None
    assert record["turn_count"] >= 1
    # #45: clean run has zero permission denials surfaced.
    assert record["permission_denials_count"] == 0


# -- Permission denials surfaced (#45) --------------------------------------


async def test_permission_denials_surfaced_in_terminal_json(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A turn_completed event whose payload carries ``permission_denials``
    must surface the count in ``terminal.json`` AND emit a WARNING log so
    operators do not mistake misleading-success for a clean unattended
    completion. Repro of the leader E2E #42 misleading-success case under
    ``permission_mode: acceptEdits``."""
    import logging as _logging

    script = FakeTurnScript(
        events=[
            ("message_delta", {"text": "I cannot run shell commands.", "block_index": 0}),
            (
                "turn_completed",
                {
                    "duration_ms": 1,
                    "result": "I would run `ls` but lack permission.",
                    "permission_denials": [
                        {"tool_name": "Bash", "tool_use_id": "tu_1"},
                        {"tool_name": "AskUserQuestion", "tool_use_id": "tu_2"},
                    ],
                },
            ),
        ],
    )
    prov = FakeProvider(default_script=script)
    orch, _ = _make(tmp_path, provider=prov)
    with caplog.at_level(_logging.WARNING, logger="symphony.orchestrator"):
        await orch.run_once()

    record = _read_terminal(tmp_path)
    assert record["terminal_state"] == "completed"
    assert record["permission_denials_count"] == 2
    # WARNING text mentions the count, the issue identifier, and the
    # operator-facing pointer to the runbook.
    warnings = [r for r in caplog.records if r.levelno == _logging.WARNING]
    permission_warnings = [r for r in warnings if "permission_denials=" in r.getMessage()]
    assert permission_warnings, (
        f"expected a permission_denials WARNING; got: {[r.getMessage() for r in warnings]}"
    )
    msg = permission_warnings[0].getMessage()
    assert "acme/proj#1" in msg
    assert "permission_mode" in msg
    assert "m3-runbook" in msg


async def test_permission_denials_count_zero_when_field_missing(tmp_path: Path) -> None:
    """The default success script has no ``permission_denials`` key.
    Count must be 0 (not absent / not raising) so operators always see
    the same field shape in terminal.json."""
    orch, _ = _make(tmp_path, provider=FakeProvider())
    await orch.run_once()
    record = _read_terminal(tmp_path)
    assert "permission_denials_count" in record
    assert record["permission_denials_count"] == 0


def test_extract_permission_denials_count_defensive() -> None:
    """Helper survives None last_event, missing payload, malformed
    permission_denials values — never raises into the worker finally
    block."""
    from datetime import datetime, timezone

    from symphony.events import AgentEvent
    from symphony.orchestrator import _extract_permission_denials_count

    # Build a minimal worker-like object with .last_event + .issue.
    class _Stub:
        def __init__(self, event):
            self.last_event = event

            class _I:
                identifier = "x/y#1"

            self.issue = _I()

    assert _extract_permission_denials_count(_Stub(None)) == 0

    # Wrong-shape payload: permission_denials is a string (would TypeError on len()
    # only if it weren't a string — strings have len. Use an int instead).
    bad = AgentEvent(
        event="turn_completed",
        timestamp=datetime.now(timezone.utc),
        session_id="s",
        provider="fake",
        issue_identifier="x/y#1",
        attempt=1,
        payload={"permission_denials": 5},  # int has no len → TypeError → 0
    )
    assert _extract_permission_denials_count(_Stub(bad)) == 0

    # Missing field entirely → 0.
    none_payload = AgentEvent(
        event="turn_completed",
        timestamp=datetime.now(timezone.utc),
        session_id="s",
        provider="fake",
        issue_identifier="x/y#1",
        attempt=1,
        payload={},
    )
    assert _extract_permission_denials_count(_Stub(none_payload)) == 0


# -- after_run hook always runs ----------------------------------------------


async def test_after_run_hook_runs_even_when_provider_fails(tmp_path: Path) -> None:
    """SPEC §14 step 11: ``after_run`` runs after the provider terminal
    state. Easiest assertion: configure the hook to write a marker file
    and verify it exists regardless of provider outcome.
    """
    cfg = _config(tmp_path)
    marker = tmp_path / "after_run_marker.txt"
    cfg = _config(tmp_path)
    # Re-create config with after_run hook pointing at the marker.
    from dataclasses import replace as _replace

    cfg = _replace(
        cfg,
        workspace=_replace(cfg.workspace, after_run=f"touch {marker}"),
    )
    bad = FakeTurnScript(events=[], raise_after=0, raise_with=ProviderError)
    prov = FakeProvider(default_script=bad)
    tracker = FakeGitHubTracker(issues=[_issue()])
    mgr = WorkspaceManager(cfg.workspace)
    orch = Orchestrator(cfg, tracker=tracker, provider=prov, workspace_manager=mgr)
    await orch.run_once()
    assert marker.exists(), "after_run hook should run even on provider failure"


# -- Smoke: redaction ---------------------------------------------------------


async def test_secrets_in_event_payload_are_redacted_in_terminal_artifacts(
    tmp_path: Path,
) -> None:
    """Tool inputs containing token-shaped strings must not survive into
    events.jsonl (already covered in test_orchestrator) — re-assert here
    as part of the cleanup acceptance set."""
    secret = "ghp_aaaabbbbccccdddd1111"
    script = FakeTurnScript(
        events=[
            ("tool_started", {"tool_name": "shell", "input": {"token": secret}}),
            ("turn_completed", {"duration_ms": 1, "result": "ok"}),
        ],
    )
    prov = FakeProvider(default_script=script)
    orch, _ = _make(tmp_path, provider=prov)
    await orch.run_once()

    events_file = next(Path(tmp_path / "artifacts").rglob("events.jsonl"))
    blob = events_file.read_text()
    assert secret not in blob
    assert "<redacted>" in blob


# -- turn_failed classification (#30) ----------------------------------------


async def test_turn_failed_default_is_retryable_and_releases_claim(tmp_path: Path) -> None:
    """SPEC §16 + #30: a turn_failed event with no auth/permission subtype
    is retryable. The orchestrator should schedule a retry and release
    the claim — NOT mark blocked.

    Concrete reproduction: Claude API 503 came back as `subtype="success"`
    (the SDK uses subtype to indicate stop reason, not retryability), so a
    plain unrecognized-subtype must default to retryable.
    """
    script = FakeTurnScript(
        events=[
            ("message_delta", {"text": "API Error 503 ..."}),
            ("turn_failed", {"subtype": "success", "error": "transient API blip"}),
        ],
    )
    prov = FakeProvider(default_script=script)
    orch, tracker = _make(tmp_path, provider=prov)
    result = await orch.run_once()

    assert result.dispatched == ["acme/proj#1"]
    assert "acme/proj#1" in result.retries_scheduled
    rs = orch.retry_states["acme/proj#1"]
    assert rs.next_attempt_at is not None  # retry scheduled

    state = tracker.states["acme/proj#1"]
    assert state.blocked is False
    assert state.claimed_by is None  # released, not blocked

    record = _read_terminal(tmp_path)
    assert record["terminal_state"] == "failed"
    assert record["retryable"] is True
    assert record["blocked"] is False


async def test_turn_failed_with_non_retryable_subtype_marks_blocked(
    tmp_path: Path,
) -> None:
    """Auth/permission/quota-style subtypes must flip to non-retryable so
    the issue gets `symphony-blocked` and a future poll won't silently
    re-dispatch."""
    script = FakeTurnScript(
        events=[
            ("turn_failed", {"subtype": "auth_failed", "error": "401 token expired"}),
        ],
    )
    prov = FakeProvider(default_script=script)
    orch, tracker = _make(tmp_path, provider=prov)
    result = await orch.run_once()

    assert "acme/proj#1" not in result.retries_scheduled
    rs = orch.retry_states["acme/proj#1"]
    assert rs.next_attempt_at is None  # no retry

    state = tracker.states["acme/proj#1"]
    assert state.blocked is True  # marked blocked
    assert state.claimed_by is None

    record = _read_terminal(tmp_path)
    assert record["retryable"] is False
    assert record["blocked"] is True


async def test_turn_failed_substring_match_for_permission_denied(
    tmp_path: Path,
) -> None:
    """Subtype the SDK might send as `claude_permission_denied` or
    `tool_permission_denied` should still flip non-retryable via the
    substring fallback."""
    script = FakeTurnScript(
        events=[
            ("turn_failed", {"subtype": "tool_permission_denied", "error": "."}),
        ],
    )
    prov = FakeProvider(default_script=script)
    orch, tracker = _make(tmp_path, provider=prov)
    await orch.run_once()
    assert tracker.states["acme/proj#1"].blocked is True


async def test_provider_emitted_turn_cancelled_is_retryable(tmp_path: Path) -> None:
    """A provider-emitted turn_cancelled (no orchestrator timeout context)
    should also schedule a retry so the issue can be picked up next tick."""
    script = FakeTurnScript(
        events=[
            ("turn_cancelled", {"subtype": "interrupt", "source": "provider"}),
        ],
    )
    prov = FakeProvider(default_script=script)
    orch, tracker = _make(tmp_path, provider=prov)
    result = await orch.run_once()

    assert "acme/proj#1" in result.retries_scheduled
    state = tracker.states["acme/proj#1"]
    assert state.blocked is False
    assert state.claimed_by is None


def test_classify_turn_failed_helper_is_defensive() -> None:
    """`_classify_turn_failed(None)` and weird-shape events must return a
    safe default rather than raise — the worker has to terminate cleanly
    even if the event log got malformed."""
    from symphony.orchestrator import _classify_turn_failed

    retryable, reason = _classify_turn_failed(None)
    assert retryable is True
    assert "turn_failed" in reason

    # Wrong event name → still safe.
    other = AgentEvent(
        event="message_delta",
        timestamp=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        session_id="s",
        provider="fake",
        issue_identifier="x",
        attempt=1,
    )
    retryable, _ = _classify_turn_failed(other)
    assert retryable is True
