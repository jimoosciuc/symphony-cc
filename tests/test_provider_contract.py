"""Provider contract tests (issue #12).

Parametrizes the same protocol-level assertions over both
:class:`~symphony.provider.fake.FakeProvider` and the real
:class:`~symphony.provider.claude_code.ClaudeCodeProvider` (driven by an
injected fake SDK client). The goal is that any future provider
implementation can be wired into the same ``PROVIDER_BUILDERS`` table
and the existing tests will validate it without modification.

What's tested at the contract level:

- :func:`AgentProviderProtocol.start_session` returns a SessionRecord
  whose envelope matches SPEC §5.4 and emits NO events.
- ``send_input`` is an async generator: the first event of the first
  call after ``start_session`` is ``session_started``; the first event
  after ``restore`` is ``session_restored``.
- Every emitted event satisfies the SPEC §17 envelope schema
  (provider, session_id, attempt, payload, timestamp, etc.).
- The stream always ends with one of the terminal turn events
  (``turn_completed`` / ``turn_failed`` / ``turn_cancelled``).
- Multi-turn continuation: the second send_input does NOT re-emit
  ``session_started`` and ``turn_count`` increments after each turn.
- ``restore`` requires a ``provider_session_id`` and bumps ``attempt``.
- ``interrupt`` / ``cancel`` return a single AgentEvent envelope.
- ``close`` is idempotent.
- ``send_input`` after ``close`` raises :class:`ProviderError`.
- Crash mid-stream raises :class:`ProviderRetryableError` (per the
  scripted scenario's ``__raise__`` step) so the orchestrator's retry
  policy kicks in.

What's NOT tested here (covered by provider-specific test modules):

- Claude SDK exception → typed-error mapping (``test_claude_provider``).
- Per-event normalization of every SDK message class
  (``test_claude_provider``).
- Orchestrator behaviors on top of the protocol (``test_orchestrator``,
  ``test_timeouts``, ``test_recovery``).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from pathlib import Path

import pytest

from symphony.events import AgentEvent
from symphony.models import Issue
from symphony.provider.base import (
    AgentProviderProtocol,
    ProviderError,
    ProviderRestoreError,
    ProviderRetryableError,
)
from tests._provider_contract import (
    assert_event_envelope_shape,
    assert_session_record_shape,
    list_scenarios,
    load_scenario,
    make_claude_provider_for,
    make_fake_provider_for,
)

# -- Parametrize table -------------------------------------------------------

# Each entry is ``(provider_kind, builder)``. Future providers wire into
# this table and inherit every contract assertion below.
PROVIDER_BUILDERS: list[tuple[str, Callable]] = [
    ("fake", make_fake_provider_for),
    ("claude_code", make_claude_provider_for),
]
PROVIDER_IDS = [kind for kind, _ in PROVIDER_BUILDERS]


def _issue(*, number: int = 1) -> Issue:
    return Issue(
        id=f"I_{number}",
        number=number,
        identifier=f"acme/proj#{number}",
        owner="acme",
        repo="proj",
        title=f"contract t{number}",
        body="b",
        state="open",
        url=f"https://github.com/acme/proj/issues/{number}",
    )


async def _drain(stream: AsyncIterator[AgentEvent]) -> list[AgentEvent]:
    return [event async for event in stream]


# -- Discovery ---------------------------------------------------------------


def test_all_fixtures_load() -> None:
    """Every JSONL fixture parses cleanly and has at least one step."""
    names = list_scenarios()
    assert names, "fixture directory is empty — did the provider_events/ files move?"
    for name in names:
        sc = load_scenario(name)
        assert sc.steps, f"scenario {name!r} has no steps"


def test_known_fixture_names_are_present() -> None:
    """The set of fixtures is part of the contract — provider authors
    should be able to run the named scenarios without rediscovering them."""
    names = set(list_scenarios())
    expected = {
        "normal_completion",
        "tool_round_trip",
        "permission_request",
        "usage_heartbeat",
        "rate_limit",
        "malformed",
        "cancelled",
        "crash_after_partial",
        "turn_failed",
    }
    missing = expected - names
    assert not missing, f"missing required contract fixtures: {sorted(missing)}"


# -- Protocol surface --------------------------------------------------------


@pytest.mark.parametrize("provider_kind, build", PROVIDER_BUILDERS, ids=PROVIDER_IDS)
def test_provider_satisfies_protocol(
    provider_kind: str, build: Callable, tmp_path: Path
) -> None:
    """All implementations must satisfy AgentProviderProtocol structurally.

    Asserts via ``isinstance`` against the runtime-checkable protocol —
    this is the cheapest signal that a provider has not drifted.
    """
    del tmp_path
    provider, _ = build(None)
    assert isinstance(provider, AgentProviderProtocol)
    assert provider.name == provider_kind


# -- start_session -----------------------------------------------------------


@pytest.mark.parametrize("provider_kind, build", PROVIDER_BUILDERS, ids=PROVIDER_IDS)
async def test_start_session_returns_record_and_emits_no_events(
    provider_kind: str, build: Callable, tmp_path: Path
) -> None:
    """SPEC §10: ``start_session`` connects the client and returns a
    SessionRecord. It MUST NOT yield events — the first events arrive
    only via ``send_input``."""
    del provider_kind
    provider, cfg_factory = build(load_scenario("normal_completion"))
    issue = _issue()
    cfg = cfg_factory(tmp_path)

    record = await provider.start_session(issue, tmp_path / "ws", cfg)
    assert_session_record_shape(record)
    assert record.provider == provider.name
    assert record.issue_identifier == issue.identifier
    assert record.attempt == 1
    assert record.turn_count == 0
    assert record.terminal_state is None


# -- send_input: shape -------------------------------------------------------


@pytest.mark.parametrize("provider_kind, build", PROVIDER_BUILDERS, ids=PROVIDER_IDS)
async def test_first_send_input_yields_session_started_then_terminal(
    provider_kind: str, build: Callable, tmp_path: Path
) -> None:
    """First send_input after start_session emits ``session_started`` as
    its first event and the stream eventually ends with a terminal
    event."""
    del provider_kind
    scenario = load_scenario("normal_completion")
    provider, cfg_factory = build(scenario)
    cfg = cfg_factory(tmp_path)
    issue = _issue()
    record = await provider.start_session(issue, tmp_path / "ws", cfg)

    events = await _drain(provider.send_input(record, "go"))
    assert events, "send_input emitted no events"
    assert events[0].event == "session_started"
    assert events[-1].event in {"turn_completed", "turn_failed", "turn_cancelled"}


@pytest.mark.parametrize("provider_kind, build", PROVIDER_BUILDERS, ids=PROVIDER_IDS)
async def test_every_emitted_event_has_required_envelope(
    provider_kind: str, build: Callable, tmp_path: Path
) -> None:
    """Schema check: every event yielded by every provider satisfies
    SPEC §17's required envelope fields."""
    del provider_kind
    provider, cfg_factory = build(load_scenario("normal_completion"))
    cfg = cfg_factory(tmp_path)
    record = await provider.start_session(_issue(), tmp_path / "ws", cfg)
    for ev in await _drain(provider.send_input(record, "msg")):
        assert_event_envelope_shape(ev, provider_name=provider.name)
        assert ev.session_id == record.session_id


@pytest.mark.parametrize("provider_kind, build", PROVIDER_BUILDERS, ids=PROVIDER_IDS)
async def test_continuation_send_input_does_not_re_emit_session_started(
    provider_kind: str, build: Callable, tmp_path: Path
) -> None:
    """Per docs/claude-provider.md §2.1: ``session_started`` fires once
    per attempt. A second send_input continues the same logical session
    and must NOT re-emit it."""
    del provider_kind
    provider, cfg_factory = build(load_scenario("normal_completion"))
    cfg = cfg_factory(tmp_path)
    record = await provider.start_session(_issue(), tmp_path / "ws", cfg)

    first = await _drain(provider.send_input(record, "first"))
    second = await _drain(provider.send_input(record, "second"))
    assert first[0].event == "session_started"
    assert all(e.event != "session_started" for e in second), (
        f"second send_input re-emitted session_started: {[e.event for e in second]}"
    )


# -- send_input: scenario sweep ---------------------------------------------


_NON_TERMINAL_FIXTURES = [
    "normal_completion",
    "tool_round_trip",
    "permission_request",
    "usage_heartbeat",
    "rate_limit",
    "malformed",
    "cancelled",
    "turn_failed",
]


@pytest.mark.parametrize("provider_kind, build", PROVIDER_BUILDERS, ids=PROVIDER_IDS)
@pytest.mark.parametrize("scenario_name", _NON_TERMINAL_FIXTURES)
async def test_scenario_stream_always_ends_with_terminal_event(
    provider_kind: str, build: Callable, scenario_name: str, tmp_path: Path
) -> None:
    """Every scripted scenario terminates the stream with one of the
    three terminal turn events. Catches providers that drop or rename
    terminals during normalization."""
    del provider_kind
    scenario = load_scenario(scenario_name)
    provider, cfg_factory = build(scenario)
    cfg = cfg_factory(tmp_path)
    record = await provider.start_session(_issue(), tmp_path / "ws", cfg)
    events = await _drain(provider.send_input(record, "go"))
    assert events, f"scenario {scenario_name} produced no events"
    assert events[-1].event in {"turn_completed", "turn_failed", "turn_cancelled"}, (
        f"scenario {scenario_name} ended with {events[-1].event!r}"
    )


@pytest.mark.parametrize("provider_kind, build", PROVIDER_BUILDERS, ids=PROVIDER_IDS)
async def test_crash_scenario_raises_retryable(
    provider_kind: str, build: Callable, tmp_path: Path
) -> None:
    """``crash_after_partial`` raises :class:`ProviderRetryableError`
    mid-stream. Both providers route their underlying crash into
    that typed error so the orchestrator can apply RetryConfig."""
    del provider_kind
    scenario = load_scenario("crash_after_partial")
    provider, cfg_factory = build(scenario)
    cfg = cfg_factory(tmp_path)
    record = await provider.start_session(_issue(), tmp_path / "ws", cfg)
    with pytest.raises(ProviderRetryableError):
        await _drain(provider.send_input(record, "go"))


# -- restore ----------------------------------------------------------------


@pytest.mark.parametrize("provider_kind, build", PROVIDER_BUILDERS, ids=PROVIDER_IDS)
async def test_restore_requires_provider_session_id(
    provider_kind: str, build: Callable, tmp_path: Path
) -> None:
    """Restore on a record with no provider_session_id must fail with
    a typed :class:`ProviderRestoreError` so the orchestrator can route
    to ``claude.retry_resume_policy``."""
    del provider_kind
    provider, cfg_factory = build(load_scenario("normal_completion"))
    cfg = cfg_factory(tmp_path)
    record = await provider.start_session(_issue(), tmp_path / "ws", cfg)
    # provider_session_id stays None until the first send_input runs.
    assert record.provider_session_id is None
    with pytest.raises(ProviderRestoreError):
        await provider.restore(record)


@pytest.mark.parametrize("provider_kind, build", PROVIDER_BUILDERS, ids=PROVIDER_IDS)
async def test_restore_bumps_attempt_and_first_event_is_session_restored(
    provider_kind: str, build: Callable, tmp_path: Path
) -> None:
    """After a successful restore, ``attempt`` is incremented and the
    next send_input emits ``session_restored`` (NOT ``session_started``)
    as its first event."""
    del provider_kind
    provider, cfg_factory = build(load_scenario("normal_completion"))
    cfg = cfg_factory(tmp_path)
    record = await provider.start_session(_issue(), tmp_path / "ws", cfg)

    # Drain first attempt to populate provider_session_id.
    await _drain(provider.send_input(record, "first"))
    assert record.provider_session_id is not None
    attempt_before = record.attempt

    restored = await provider.restore(record)
    assert restored.attempt == attempt_before + 1

    # For Claude provider, restore() reuses the same session_id but
    # opens a fresh client; both providers reset the per-attempt
    # "saw first event" flag so the next send_input emits
    # session_restored.
    events = await _drain(provider.send_input(restored, "after-restore"))
    assert events[0].event == "session_restored", (
        f"first event after restore was {events[0].event!r}, expected session_restored"
    )


# -- interrupt / cancel / close ---------------------------------------------


@pytest.mark.parametrize("provider_kind, build", PROVIDER_BUILDERS, ids=PROVIDER_IDS)
async def test_interrupt_returns_event_envelope(
    provider_kind: str, build: Callable, tmp_path: Path
) -> None:
    """``interrupt`` returns a single AgentEvent (typically
    ``turn_cancelled``) — the orchestrator records it as a synthesized
    event when it stops a stalled turn."""
    del provider_kind
    provider, cfg_factory = build(load_scenario("normal_completion"))
    cfg = cfg_factory(tmp_path)
    record = await provider.start_session(_issue(), tmp_path / "ws", cfg)

    event = await provider.interrupt(record)
    assert isinstance(event, AgentEvent)
    assert_event_envelope_shape(event, provider_name=provider.name)


@pytest.mark.parametrize("provider_kind, build", PROVIDER_BUILDERS, ids=PROVIDER_IDS)
async def test_cancel_marks_terminal_and_returns_event(
    provider_kind: str, build: Callable, tmp_path: Path
) -> None:
    """``cancel`` disconnects the underlying client and returns the
    closing event envelope."""
    del provider_kind
    provider, cfg_factory = build(load_scenario("normal_completion"))
    cfg = cfg_factory(tmp_path)
    record = await provider.start_session(_issue(), tmp_path / "ws", cfg)

    event = await provider.cancel(record)
    assert isinstance(event, AgentEvent)
    assert_event_envelope_shape(event, provider_name=provider.name)


@pytest.mark.parametrize("provider_kind, build", PROVIDER_BUILDERS, ids=PROVIDER_IDS)
async def test_close_is_idempotent(
    provider_kind: str, build: Callable, tmp_path: Path
) -> None:
    """``close`` may be called multiple times without raising. The
    orchestrator calls it from a finally block and may see the session
    already closed by ``cancel``."""
    del provider_kind
    provider, cfg_factory = build(load_scenario("normal_completion"))
    cfg = cfg_factory(tmp_path)
    record = await provider.start_session(_issue(), tmp_path / "ws", cfg)
    await provider.close(record)
    # Second close MUST NOT raise.
    await provider.close(record)


@pytest.mark.parametrize("provider_kind, build", PROVIDER_BUILDERS, ids=PROVIDER_IDS)
async def test_send_input_after_close_raises_provider_error(
    provider_kind: str, build: Callable, tmp_path: Path
) -> None:
    """The session is dead after ``close`` (or ``cancel``). Calling
    send_input on it must raise :class:`ProviderError` so the
    orchestrator does not silently swallow the lifecycle violation."""
    del provider_kind
    provider, cfg_factory = build(load_scenario("normal_completion"))
    cfg = cfg_factory(tmp_path)
    record = await provider.start_session(_issue(), tmp_path / "ws", cfg)
    await provider.cancel(record)
    with pytest.raises(ProviderError):
        await _drain(provider.send_input(record, "after-cancel"))


# -- session record bookkeeping ---------------------------------------------


@pytest.mark.parametrize("provider_kind, build", PROVIDER_BUILDERS, ids=PROVIDER_IDS)
async def test_turn_count_increments_after_terminal_event(
    provider_kind: str, build: Callable, tmp_path: Path
) -> None:
    """The provider must update ``session.turn_count`` to reflect
    completed turns. The orchestrator's max-turns guard depends on
    this count."""
    del provider_kind
    provider, cfg_factory = build(load_scenario("normal_completion"))
    cfg = cfg_factory(tmp_path)
    record = await provider.start_session(_issue(), tmp_path / "ws", cfg)
    await _drain(provider.send_input(record, "first"))
    assert record.turn_count >= 1
    await _drain(provider.send_input(record, "second"))
    assert record.turn_count >= 2


@pytest.mark.parametrize("provider_kind, build", PROVIDER_BUILDERS, ids=PROVIDER_IDS)
async def test_provider_session_id_populated_on_first_send_input(
    provider_kind: str, build: Callable, tmp_path: Path
) -> None:
    """``provider_session_id`` is set on the record by the time the
    first send_input completes. Restart recovery + cross-attempt
    restore depend on this being live before the daemon may crash."""
    del provider_kind
    provider, cfg_factory = build(load_scenario("normal_completion"))
    cfg = cfg_factory(tmp_path)
    record = await provider.start_session(_issue(), tmp_path / "ws", cfg)
    assert record.provider_session_id is None
    await _drain(provider.send_input(record, "first"))
    assert record.provider_session_id is not None
    assert isinstance(record.provider_session_id, str)


# -- Scenario loader edge cases ---------------------------------------------


def test_load_scenario_carries_raise_step() -> None:
    sc = load_scenario("crash_after_partial")
    assert sc.raise_at_end is not None
    assert sc.raise_at_end.error == "ProviderRetryableError"


def test_load_scenario_terminal_event_helper() -> None:
    assert load_scenario("normal_completion").terminal_event == "turn_completed"
    assert load_scenario("turn_failed").terminal_event == "turn_failed"
    assert load_scenario("cancelled").terminal_event == "turn_cancelled"
    # Crash-only scenario has no terminal — orchestrator catches the raise.
    assert load_scenario("crash_after_partial").terminal_event is None
