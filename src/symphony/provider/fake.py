"""Scripted fake provider for orchestrator + provider-contract tests.

The fake honors :class:`~symphony.provider.base.AgentProviderProtocol`:
``start_session`` connects (no events), ``send_input`` yields events
according to a per-turn script. Tests configure scripts to drive specific
behaviors — happy path, retryable failure, terminal failure, stall, etc.

Why scripted (not record/replay): orchestrator tests want fine-grained
control over event ordering and timing, including injecting failures
that real Claude rarely produces. A live record would also bind tests
to vendor SDK shapes, defeating the boundary the protocol exists for.
"""

from __future__ import annotations

import asyncio
import uuid
from collections import defaultdict
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from symphony.config import ClaudeConfig
from symphony.events import AgentEvent
from symphony.models import Issue
from symphony.provider.base import (
    ProviderError,
    ProviderRestoreError,
    SessionRecord,
    Terminal,
)


@dataclass
class FakeTurnScript:
    """One turn's scripted output for the fake provider.

    Each entry in ``events`` is ``(event_name, payload)``. The fake
    auto-synthesizes the ``session_started`` event on the first turn and
    the terminal envelope (``session_id``, ``provider``, ``attempt``,
    timestamp) on every event.

    Use ``raise_after`` to make the turn yield N events and then raise
    a typed provider error — useful for testing crash and retry paths.
    """

    events: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    raise_after: int | None = None
    raise_with: type[ProviderError] = ProviderError
    raise_message: str = "scripted failure"


@dataclass
class FakeSessionState:
    """Provider-private state per :class:`SessionRecord`."""

    record: SessionRecord
    closed: bool = False
    interrupt_signaled: bool = False
    turn_index: int = 0  # increments on each send_input call


class FakeProvider:
    """In-memory scripted provider.

    Construction takes a default script for any session/turn that wasn't
    individually configured; tests can override per-(session_id, turn)
    via :meth:`set_turn_script`.
    """

    name = "fake"

    def __init__(
        self,
        default_script: FakeTurnScript | None = None,
        *,
        provider_session_id: str | None = None,
        restore_should_fail: bool = False,
    ) -> None:
        self._default_script = default_script or _default_success_script()
        self._scripts: dict[tuple[str, int], FakeTurnScript] = {}
        self._sessions: dict[str, FakeSessionState] = {}
        # Auto-generate a stable id so tests can assert reuse across turns;
        # tests that need a known value can pass one in.
        self._next_provider_id = provider_session_id
        self.restore_should_fail = restore_should_fail
        self.calls: list[tuple[str, str]] = []  # (method, session_id) for assertions

    # -- Test setup ----------------------------------------------------------

    def set_turn_script(self, session_id: str, turn: int, script: FakeTurnScript) -> None:
        self._scripts[(session_id, turn)] = script

    def script_for(self, session_id: str, turn: int) -> FakeTurnScript:
        return self._scripts.get((session_id, turn), self._default_script)

    # -- Protocol surface ----------------------------------------------------

    async def start_session(
        self,
        issue: Issue,
        workspace_path: Path,
        config: ClaudeConfig,
    ) -> SessionRecord:
        # Per SPEC §10 + docs/claude-provider.md §2.1: connect only,
        # no events streamed, no first-prompt arg.
        session_id = f"sym-{uuid.uuid4().hex[:12]}"
        record = SessionRecord(
            session_id=session_id,
            provider=self.name,
            issue_identifier=issue.identifier,
            issue_number=issue.number,
            workspace_path=workspace_path,
            artifact_dir=Path(config.artifact_store)
            / f"{issue.owner}_{issue.repo}_{issue.number}"
            / "1",
            started_at=_now(),
        )
        self._sessions[session_id] = FakeSessionState(record=record)
        self.calls.append(("start_session", session_id))
        return record

    async def restore(self, session_record: SessionRecord) -> SessionRecord:
        # Record the call BEFORE raising so tests can assert restore was
        # attempted even when scripted to fail.
        self.calls.append(("restore", session_record.session_id))
        if self.restore_should_fail:
            raise ProviderRestoreError("scripted restore failure")
        if session_record.provider_session_id:
            session_record.previous_provider_session_ids.append(session_record.provider_session_id)
        # restore() does not stream events; it just bumps the attempt
        # counter and re-registers the session. Reset turn_count to 0 so
        # the next send_input synthesizes session_restored as its first
        # event (per docs/claude-provider.md §2.1).
        session_record.attempt += 1
        session_record.turn_count = 0
        session_record.last_event_at = _now()
        # Re-register state so subsequent send_input/cancel work.
        self._sessions[session_record.session_id] = FakeSessionState(record=session_record)
        return session_record

    async def send_input(
        self,
        session: SessionRecord,
        message: str,
    ) -> AsyncIterator[AgentEvent]:
        """Async generator: callers do ``async for ev in provider.send_input(...)``.

        Defined as ``async def`` with ``yield``s, which Python recognizes as
        an async generator function (returns an :class:`AsyncIterator`
        without an extra ``await``).
        """
        state = self._sessions.get(session.session_id)
        if state is None or state.closed:
            raise ProviderError(f"session {session.session_id} is not open")
        self.calls.append(("send_input", session.session_id))
        turn = state.turn_index + 1
        state.turn_index = turn
        script = self.script_for(session.session_id, turn)

        # First turn after start_session synthesizes session_started and
        # captures provider_session_id. First turn after restore would
        # synthesize session_restored, distinguished by the presence of
        # previous_provider_session_ids on the record.
        is_first_turn_of_attempt = session.turn_count == 0
        # Increment turn_count BEFORE any yields so that an orchestrator
        # that breaks out of the async-for after a terminal event still
        # records the turn (the rest of the generator body would never
        # run otherwise).
        session.turn_count += 1
        if is_first_turn_of_attempt:
            session.provider_session_id = self._allocate_provider_session_id()
            event_name = (
                "session_restored" if session.previous_provider_session_ids else "session_started"
            )
            yield self._envelope(
                event=event_name,
                session=session,
                payload={
                    "model": "fake-model",
                    "session_id": session.provider_session_id,
                },
            )

        # raise_after == 0: raise immediately, no events.
        if script.raise_after == 0:
            raise script.raise_with(script.raise_message)

        # Yield scripted events; raise after N if configured.
        for i, (name, payload) in enumerate(script.events):
            if state.interrupt_signaled and not _is_terminal(name):
                # Drop pending events and yield a turn_cancelled instead.
                yield self._envelope(
                    event="turn_cancelled",
                    session=session,
                    payload={
                        "subtype": "interrupt",
                        "events_dropped": len(script.events) - i,
                    },
                )
                state.interrupt_signaled = False
                return
            yield self._envelope(event=name, session=session, payload=dict(payload))
            if script.raise_after is not None and i + 1 >= script.raise_after:
                raise script.raise_with(script.raise_message)

    async def interrupt(self, session: SessionRecord) -> AgentEvent:
        state = self._sessions[session.session_id]
        state.interrupt_signaled = True
        self.calls.append(("interrupt", session.session_id))
        return self._envelope(
            event="turn_cancelled",
            session=session,
            payload={"subtype": "interrupt"},
        )

    async def cancel(self, session: SessionRecord) -> AgentEvent:
        state = self._sessions[session.session_id]
        state.interrupt_signaled = True
        state.closed = True
        session.terminal_state = Terminal.CANCELLED
        self.calls.append(("cancel", session.session_id))
        return self._envelope(
            event="session_closed",
            session=session,
            payload={"reason": "cancel"},
        )

    async def close(self, session: SessionRecord) -> None:
        state = self._sessions.get(session.session_id)
        if state is not None:
            state.closed = True
        self.calls.append(("close", session.session_id))

    # -- internals -----------------------------------------------------------

    def _allocate_provider_session_id(self) -> str:
        if self._next_provider_id:
            return self._next_provider_id
        return f"fake-pid-{uuid.uuid4().hex[:8]}"

    def _envelope(
        self,
        *,
        event: str,
        session: SessionRecord,
        payload: dict[str, Any],
    ) -> AgentEvent:
        ev = AgentEvent(
            event=event,
            timestamp=_now(),
            session_id=session.session_id,
            provider=self.name,
            issue_identifier=session.issue_identifier,
            attempt=session.attempt,
            payload=payload,
            provider_session_id=session.provider_session_id,
        )
        session.last_event_at = ev.timestamp
        if event == "turn_completed":
            session.terminal_state = None  # turn done, session continues
        return ev


# -- Helpers ------------------------------------------------------------------


def _is_terminal(event_name: str) -> bool:
    return event_name in {"turn_completed", "turn_failed", "turn_cancelled"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _default_success_script() -> FakeTurnScript:
    return FakeTurnScript(
        events=[
            ("message_delta", {"text": "ok", "block_index": 0}),
            ("message_completed", {"stop_reason": "end_turn"}),
            ("turn_completed", {"duration_ms": 1, "result": "ok"}),
        ],
    )


# Help static analysis understand defaultdict above is intentional.
__all__ = ["FakeProvider", "FakeTurnScript"]

# Silence unused-import warning when defaultdict isn't actually used.
_ = defaultdict, asyncio
