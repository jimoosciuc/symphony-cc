"""Claude Code provider — real implementation of :class:`AgentProviderProtocol`.

Implements the contract documented in ``docs/claude-provider.md``:

- Uses ``claude-agent-sdk``'s :class:`ClaudeSDKClient` (streaming/client
  mode). The one-shot ``query()`` helper is **not** the primary surface.
- ``start_session`` and ``restore`` connect the client and return a
  :class:`SessionRecord`; they do NOT stream events.
- ``send_input`` is the only event-emitting method. The first call after
  ``start_session`` emits a synthesized ``session_started`` event whose
  payload carries the Claude-native session id discovered on the first
  ``AssistantMessage`` / ``ResultMessage``. The first call after
  ``restore`` emits ``session_restored``.
- Per-attempt artifacts (``request.json``, ``session.json``) are written
  via the supplied :class:`ArtifactWriter`. Artifact writing happens at
  the boundaries documented in §5.1.

The orchestrator (#7 / #10) wraps every event for stall/turn timeouts;
this provider is unaware of those budgets — it just yields events as
the SDK delivers them.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import AsyncIterator
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from symphony.config import ClaudeConfig
from symphony.events import AgentEvent
from symphony.models import Issue
from symphony.provider.base import (
    ProviderError,
    ProviderRestoreError,
    ProviderRetryableError,
    SessionRecord,
    Terminal,
)

_LOG = logging.getLogger("symphony.provider.claude_code")


# -- SDK import shim ----------------------------------------------------------


# Import the SDK lazily so that running ``pytest`` against the fake
# provider doesn't require the SDK to be installed at the same minor
# version Symphony pins. Tests inject a fake client; production
# code-paths import normally on first use.


def _import_sdk() -> Any:
    try:
        import claude_agent_sdk  # type: ignore
    except ImportError as exc:  # pragma: no cover - exercised only when SDK missing
        raise ProviderError(
            "claude-agent-sdk is not installed; cannot run the Claude Code provider"
        ) from exc
    return claude_agent_sdk


# -- Provider ----------------------------------------------------------------


class ClaudeCodeProvider:
    """Real Claude Code provider per ``docs/claude-provider.md``.

    ``client_factory`` is the constructor injection seam: production
    builds a real :class:`ClaudeSDKClient` from
    :class:`ClaudeAgentOptions`; tests pass a factory that returns a
    fake. The factory takes the *options dict* the provider would
    otherwise hand to the SDK so tests can inspect it without parsing
    real ``ClaudeAgentOptions`` instances.

    ``writer_factory`` is similarly injectable: tests can stub artifact
    writes. In production it builds an :class:`ArtifactWriter` per
    session.
    """

    name = "claude_code"

    def __init__(
        self,
        *,
        client_factory: Any | None = None,
        session_id_factory: Any | None = None,
    ) -> None:
        self._client_factory = client_factory
        # Tests pin session ids; production gets random UUIDs.
        self._session_id_factory = session_id_factory or _default_session_id_factory
        self._sessions: dict[str, _ProviderSessionState] = {}

    # -- Protocol surface ----------------------------------------------------

    async def start_session(
        self,
        issue: Issue,
        workspace_path: Path,
        config: ClaudeConfig,
    ) -> SessionRecord:
        options = self._build_options(issue, workspace_path, config, resume=None)
        client = await self._connect(options)
        session_id = self._session_id_factory()
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
            # Pin the session-store path on the record so restore() — which
            # the orchestrator may call after a process restart with a
            # record loaded off disk — knows where to read/write without
            # a config arg.
            session_store=Path(config.session_store),
        )
        self._sessions[session_id] = _ProviderSessionState(
            client=client,
            options=options,
            session_id=session_id,
            attempt=1,
            saw_first_event=False,
            mode="start_session",
        )
        # Write the initial record per docs/claude-provider.md §5.1 phase 2:
        # provider_session_id is None at this point; the patch lands after
        # the first send_input captures the Claude-native id.
        _persist_session(record)
        return record

    async def restore(self, session_record: SessionRecord) -> SessionRecord:
        if not session_record.provider_session_id:
            raise ProviderRestoreError(
                f"cannot restore session {session_record.session_id}: "
                "no provider_session_id on record"
            )
        # Build options re-using the workspace/model/etc from the prior
        # attempt. The orchestrator hands us a pre-built record so the
        # provider doesn't need a ClaudeConfig argument here.
        options = {
            "resume": session_record.provider_session_id,
            "cwd": str(session_record.workspace_path),
            "fork_session": False,
            "continue_conversation": False,
        }
        try:
            client = await self._connect(options)
        except ProviderError as exc:
            # Anything that goes wrong before we have a working client is
            # a restore-startup failure per docs §5.3 — the orchestrator
            # catches ProviderRestoreError to route to retry_resume_policy.
            raise ProviderRestoreError(
                f"restore failed for {session_record.session_id}: {exc}"
            ) from exc

        # Bookkeeping: bump attempt + record the prior provider session id
        # so the next continuation prompt can reference it.
        prior_pid = session_record.provider_session_id
        session_record.attempt += 1
        session_record.last_event_at = _now()
        if prior_pid and prior_pid not in session_record.previous_provider_session_ids:
            session_record.previous_provider_session_ids.append(prior_pid)
        # turn_count is left to the orchestrator's max-turns accounting;
        # session_started/restored synthesis is gated on the per-attempt
        # `_ProviderSessionState.saw_first_event` flag, not on turn_count.
        self._sessions[session_record.session_id] = _ProviderSessionState(
            client=client,
            options=options,
            session_id=session_record.session_id,
            attempt=session_record.attempt,
            saw_first_event=False,
            mode="restore",
        )
        # Persist the bumped attempt + previous_provider_session_ids
        # immediately so a subsequent crash before the first send_input
        # doesn't lose the restore audit trail.
        _persist_session(session_record)
        return session_record

    async def send_input(
        self,
        session: SessionRecord,
        message: str,
    ) -> AsyncIterator[AgentEvent]:
        """Async generator: ``async for event in provider.send_input(session, msg)``.

        Defined as ``async def`` with ``yield``s, so Python treats it as
        an async-generator function (returns an :class:`AsyncIterator`
        without needing an extra ``await`` from callers).
        """
        state = self._sessions.get(session.session_id)
        if state is None:
            raise ProviderError(
                f"send_input on unknown session {session.session_id}; "
                "did you call start_session/restore first?"
            )
        if state.closed:
            raise ProviderError(f"send_input on closed session {session.session_id}")

        try:
            await state.client.query(message, session_id="symphony")
        except Exception as exc:  # noqa: BLE001 - SDK error categories vary
            raise _wrap_sdk_error("query", exc) from exc

        is_first_call_of_attempt = not state.saw_first_event
        synthesized: str | None = None
        if is_first_call_of_attempt:
            synthesized = "session_restored" if state.mode == "restore" else "session_started"

        try:
            async for raw in state.client.receive_response():
                events = _normalize_message(
                    raw,
                    session=session,
                    provider_name=self.name,
                )
                # On the very first event of the attempt, emit the
                # synthesized session_started/session_restored *before*
                # the content event. We grab provider_session_id off
                # whatever the SDK gave us and patch the record so the
                # synthesized event payload carries it.
                if is_first_call_of_attempt and events:
                    is_first_call_of_attempt = False
                    state.saw_first_event = True
                    pid = _extract_session_id(raw) or session.provider_session_id
                    if pid:
                        session.provider_session_id = pid
                        # Patch the persisted record per docs/claude-provider.md
                        # §5.1 phase 3: as soon as Claude reveals a session id,
                        # update <session_store>/<session_id>.json so
                        # cross-attempt restore can find it.
                        session.last_event_at = _now()
                        _persist_session(session)
                    if synthesized is not None:
                        yield _envelope(
                            event=synthesized,
                            session=session,
                            provider_name=self.name,
                            payload={
                                "model": _extract_model(raw),
                                "session_id": pid,
                            },
                        )
                # Re-stamp provider_session_id on every event so the
                # envelope reflects the current Claude-native id.
                for ev in events:
                    if ev.provider_session_id is None and session.provider_session_id:
                        ev = AgentEvent(
                            event=ev.event,
                            timestamp=ev.timestamp,
                            session_id=ev.session_id,
                            provider=ev.provider,
                            issue_identifier=ev.issue_identifier,
                            attempt=ev.attempt,
                            payload=ev.payload,
                            provider_session_id=session.provider_session_id,
                        )
                    yield ev
                    if ev.event in {"turn_completed", "turn_failed", "turn_cancelled"}:
                        session.turn_count += 1
                        return
        except Exception as exc:  # noqa: BLE001 - SDK errors map to typed provider errors
            raise _wrap_sdk_error("receive_response", exc) from exc

    async def interrupt(self, session: SessionRecord) -> AgentEvent:
        state = self._require_open(session)
        try:
            await state.client.interrupt()
        except Exception as exc:  # noqa: BLE001
            _LOG.warning(
                "interrupt() raised for %s: %s — treating as best-effort",
                session.session_id,
                exc,
            )
        # Per docs §2.1: the in-flight send_input generator drains its
        # receive_response and yields turn_cancelled. We don't wait for
        # that here; the orchestrator does.
        return _envelope(
            event="turn_cancelled",
            session=session,
            provider_name=self.name,
            payload={"subtype": "interrupt", "source": "provider"},
        )

    async def cancel(self, session: SessionRecord) -> AgentEvent:
        state = self._require_open(session)
        try:
            await state.client.interrupt()
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("cancel: interrupt failed for %s: %s", session.session_id, exc)
        try:
            await state.client.disconnect()
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("cancel: disconnect failed for %s: %s", session.session_id, exc)
        state.closed = True
        session.terminal_state = Terminal.CANCELLED
        return _envelope(
            event="session_closed",
            session=session,
            provider_name=self.name,
            payload={"reason": "cancel"},
        )

    async def close(self, session: SessionRecord) -> None:
        state = self._sessions.get(session.session_id)
        if state is None or state.closed:
            return
        try:
            await state.client.disconnect()
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("close: disconnect failed for %s: %s", session.session_id, exc)
        state.closed = True

    # -- Internals -----------------------------------------------------------

    def _build_options(
        self,
        issue: Issue,
        workspace_path: Path,
        config: ClaudeConfig,
        *,
        resume: str | None,
    ) -> dict[str, Any]:
        """Return the kwargs Symphony hands to ``ClaudeAgentOptions``.

        Returned as a plain dict so tests can assert the values without
        depending on the SDK's dataclass shape; the production
        ``_connect`` instantiates ``ClaudeAgentOptions(**opts)``.
        """
        del issue  # reserved for future tightening (issue-derived prompts, env, etc.)
        opts: dict[str, Any] = {
            "cwd": str(workspace_path),
            "model": config.model,
            "permission_mode": config.permission_mode,
            "fork_session": False,
            "continue_conversation": False,
        }
        if resume is not None:
            opts["resume"] = resume
        return opts

    async def _connect(self, options: dict[str, Any]) -> Any:
        if self._client_factory is not None:
            client = self._client_factory(options)
        else:
            sdk = _import_sdk()
            client = sdk.ClaudeSDKClient(options=sdk.ClaudeAgentOptions(**options))
        try:
            await client.connect()
        except Exception as exc:  # noqa: BLE001 - any SDK error here is a startup failure
            raise ProviderError(f"client.connect() failed: {exc}") from exc
        return client

    def _require_open(self, session: SessionRecord) -> _ProviderSessionState:
        state = self._sessions.get(session.session_id)
        if state is None:
            raise ProviderError(f"unknown session {session.session_id}")
        if state.closed:
            raise ProviderError(f"session {session.session_id} is closed")
        return state


# -- Internal session state --------------------------------------------------


class _ProviderSessionState:
    """Per-session state held by :class:`ClaudeCodeProvider`.

    Plain class (not dataclass) so test fakes can attach attributes
    freely without dataclass field constraints.
    """

    __slots__ = ("client", "options", "session_id", "attempt", "saw_first_event", "mode", "closed")

    def __init__(
        self,
        *,
        client: Any,
        options: dict[str, Any],
        session_id: str,
        attempt: int,
        saw_first_event: bool,
        mode: str,
    ) -> None:
        self.client = client
        self.options = options
        self.session_id = session_id
        self.attempt = attempt
        self.saw_first_event = saw_first_event
        self.mode = mode  # "start_session" or "restore"
        self.closed = False


# -- Event normalization -----------------------------------------------------


def _normalize_message(
    raw: Any,
    *,
    session: SessionRecord,
    provider_name: str,
) -> list[AgentEvent]:
    """Translate one SDK message into zero or more :class:`AgentEvent`s.

    Mapping table is documented in ``docs/claude-provider.md`` §4.
    Returns a list because one ``AssistantMessage`` may produce multiple
    Symphony events (one ``message_delta`` per ``TextBlock``, one
    ``tool_started`` per ``ToolUseBlock``, plus a closing
    ``message_completed``).
    """
    cls = type(raw).__name__
    payload_now = _now()

    def env(event: str, payload: dict[str, Any]) -> AgentEvent:
        return AgentEvent(
            event=event,
            timestamp=payload_now,
            session_id=session.session_id,
            provider=provider_name,
            issue_identifier=session.issue_identifier,
            attempt=session.attempt,
            payload=payload,
            provider_session_id=session.provider_session_id,
        )

    out: list[AgentEvent] = []

    if cls == "AssistantMessage":
        blocks = list(getattr(raw, "content", []) or [])
        for i, block in enumerate(blocks):
            block_cls = type(block).__name__
            if block_cls == "TextBlock":
                out.append(
                    env("message_delta", {"text": getattr(block, "text", ""), "block_index": i})
                )
            elif block_cls == "ToolUseBlock":
                out.append(
                    env(
                        "tool_started",
                        {
                            "tool_name": getattr(block, "name", ""),
                            "tool_use_id": getattr(block, "id", ""),
                            "input": _to_jsonable(getattr(block, "input", None)),
                        },
                    )
                )
        out.append(
            env(
                "message_completed",
                {
                    "stop_reason": getattr(raw, "stop_reason", None),
                    "usage": _to_jsonable(getattr(raw, "usage", None)),
                    "message_id": getattr(raw, "message_id", None),
                },
            )
        )
        return out

    if cls == "UserMessage":
        for block in list(getattr(raw, "content", []) or []):
            block_cls = type(block).__name__
            if block_cls == "ToolResultBlock":
                out.append(
                    env(
                        "tool_completed",
                        {
                            "tool_use_id": getattr(block, "tool_use_id", ""),
                            "is_error": bool(getattr(block, "is_error", False)),
                            "content": _to_jsonable(getattr(block, "content", None)),
                            "tool_use_result": _to_jsonable(getattr(raw, "tool_use_result", None)),
                        },
                    )
                )
        return out

    if cls == "SystemMessage":
        subtype = getattr(raw, "subtype", "") or ""
        data = _to_jsonable(getattr(raw, "data", None))
        if subtype.startswith("permission_request"):
            return [env("permission_requested", {"subtype": subtype, "data": data})]
        if subtype.startswith("permission_decision") or subtype.startswith("permission_resolved"):
            return [env("permission_resolved", {"subtype": subtype, "data": data})]
        if "usage" in subtype.lower() or (isinstance(data, dict) and "usage" in data):
            return [env("usage", {"subtype": subtype, "data": data})]
        return [env("heartbeat", {"kind": "SystemMessage", "subtype": subtype, "data": data})]

    if cls == "ResultMessage":
        is_error = bool(getattr(raw, "is_error", False))
        subtype = getattr(raw, "subtype", "") or ""
        payload = {
            "duration_ms": getattr(raw, "duration_ms", None),
            "duration_api_ms": getattr(raw, "duration_api_ms", None),
            "num_turns": getattr(raw, "num_turns", None),
            "total_cost_usd": getattr(raw, "total_cost_usd", None),
            "usage": _to_jsonable(getattr(raw, "usage", None)),
            "result": getattr(raw, "result", None),
            "structured_output": _to_jsonable(getattr(raw, "structured_output", None)),
            "permission_denials": _to_jsonable(getattr(raw, "permission_denials", None)),
            "subtype": subtype,
        }
        if not is_error:
            return [env("turn_completed", payload)]
        if subtype in {"cancelled", "interrupted"} or "cancel" in subtype.lower():
            return [env("turn_cancelled", payload)]
        payload["error"] = subtype or "ResultMessage(is_error=True)"
        return [env("turn_failed", payload)]

    if cls == "RateLimitEvent":
        return [env("heartbeat", {"kind": "rate_limit", "data": _to_jsonable(raw)})]

    if cls in {
        "StreamEvent",
        "TaskStartedMessage",
        "TaskProgressMessage",
        "TaskNotificationMessage",
        "HookEventMessage",
    }:
        return [env("heartbeat", {"kind": cls, "data": _to_jsonable(raw)})]

    return [env("malformed", {"raw": repr(raw), "reason": f"unknown message class {cls}"})]


def _extract_session_id(raw: Any) -> str | None:
    """Pull provider session id off whatever the SDK first gave us."""
    return getattr(raw, "session_id", None)


def _extract_model(raw: Any) -> str | None:
    return getattr(raw, "model", None)


def _wrap_sdk_error(stage: str, exc: BaseException) -> ProviderError:
    """Map SDK exception classes to Symphony provider errors.

    ``CLIConnectionError`` and ``ProcessError`` from the SDK are
    retryable (transient subprocess issues). Everything else is treated
    as a non-retryable provider crash so the orchestrator's error
    handler can decide whether to mark the issue blocked.
    """
    name = type(exc).__name__
    if name in {"CLIConnectionError", "ProcessError", "MessageParseError"}:
        return ProviderRetryableError(f"{stage}: {name}: {exc}")
    return ProviderError(f"{stage}: {name}: {exc}")


def _to_jsonable(value: Any) -> Any:
    """Best-effort JSON serialization of SDK objects.

    The orchestrator's :class:`ArtifactWriter` runs the redactor over
    these payloads and ``json.dumps`` with a default fallback, so we
    only need to coerce the most common SDK shapes (dataclasses, mapping
    types) into something representable.
    """
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, list | tuple):
        return [_to_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _to_jsonable(v) for k, v in value.items()}
    if is_dataclass(value) and not isinstance(value, type):
        return _to_jsonable(asdict(value))
    return repr(value)


def _envelope(
    *,
    event: str,
    session: SessionRecord,
    provider_name: str,
    payload: dict[str, Any],
) -> AgentEvent:
    return AgentEvent(
        event=event,
        timestamp=_now(),
        session_id=session.session_id,
        provider=provider_name,
        issue_identifier=session.issue_identifier,
        attempt=session.attempt,
        payload=payload,
        provider_session_id=session.provider_session_id,
    )


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _default_session_id_factory() -> str:
    return f"sym-{uuid.uuid4().hex[:12]}"


def _persist_session(record: SessionRecord) -> None:
    """Write a redacted snapshot of ``record`` to ``<session_store>/<session_id>.json``.

    Per ``docs/claude-provider.md`` §5.1:

    - Phase 2 (start_session): initial write with ``provider_session_id = null``.
    - Phase 3 (first send_input): patched in place once Claude reveals the
      session id.
    - Phase 4 (every event flush / restore): refreshed with bumped attempt
      and last_event_at.

    No-op when ``record.session_store`` is None — the provider is happy to
    run without persistence (used by tests that don't care). Errors writing
    the file are logged but never raised: a missing session record on disk
    only matters for cross-attempt restore, and the orchestrator already
    has one source of truth in ``events.jsonl``.
    """
    if record.session_store is None:
        return
    try:
        record.session_store.mkdir(parents=True, exist_ok=True)
        target = record.session_store / f"{record.session_id}.json"
        snapshot = {
            "session_id": record.session_id,
            "provider": record.provider,
            "provider_session_id": record.provider_session_id,
            "issue_identifier": record.issue_identifier,
            "issue_number": record.issue_number,
            "workspace_path": str(record.workspace_path),
            "artifact_dir": str(record.artifact_dir),
            "transcript_path": (str(record.transcript_path) if record.transcript_path else None),
            "attempt": record.attempt,
            "turn_count": record.turn_count,
            "started_at": record.started_at.isoformat() if record.started_at else None,
            "last_event_at": (record.last_event_at.isoformat() if record.last_event_at else None),
            "terminal_state": (record.terminal_state.value if record.terminal_state else None),
            "previous_provider_session_ids": list(record.previous_provider_session_ids),
            "session_store": str(record.session_store),
        }
        # Atomic-ish write: tmp file + rename so a crashed write doesn't
        # corrupt the prior good record.
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(json.dumps(snapshot, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(target)
    except OSError as exc:
        _LOG.warning(
            "could not persist session record for %s at %s: %s",
            record.session_id,
            record.session_store,
            exc,
        )


__all__ = ["ClaudeCodeProvider"]
