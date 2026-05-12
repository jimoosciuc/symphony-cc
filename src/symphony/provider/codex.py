"""Codex CLI provider for :class:`~symphony.provider.base.AgentProviderProtocol`.

The MVP uses one ``codex exec`` subprocess per turn and relies on Codex's
``thread_id`` plus ``codex exec resume`` for continuity. Raw JSONL events stay
inside this provider; Symphony sees only normalized :class:`AgentEvent`s.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from symphony.config import ClaudeConfig
from symphony.events import AgentEvent
from symphony.models import Issue
from symphony.provider.base import ProviderError, ProviderRestoreError, SessionRecord, Terminal

_LOG = logging.getLogger("symphony.provider.codex")


@dataclass(slots=True)
class CodexRunResult:
    returncode: int
    events: list[dict[str, Any]]
    stderr: str = ""
    malformed_lines: list[str] | None = None
    last_message: str | None = None


CodexRunner = Callable[
    [SessionRecord, str, ClaudeConfig, str | None],
    Awaitable[CodexRunResult],
]


class CodexProvider:
    """Provider backed by the local Codex CLI."""

    name = "codex"

    def __init__(
        self,
        *,
        runner: CodexRunner | None = None,
        session_id_factory: Callable[[], str] | None = None,
        codex_bin: str | None = None,
    ) -> None:
        self._runner = runner
        self._session_id_factory = session_id_factory or _default_session_id_factory
        self._codex_bin = codex_bin or os.environ.get("SYMPHONY_CODEX_BIN", "codex")
        self._sessions: dict[str, _CodexSessionState] = {}

    async def start_session(
        self,
        issue: Issue,
        workspace_path: Path,
        config: ClaudeConfig,
    ) -> SessionRecord:
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
            session_store=Path(config.session_store),
        )
        self._sessions[session_id] = _CodexSessionState(config=config)
        _persist_session(record)
        return record

    async def restore(self, session_record: SessionRecord) -> SessionRecord:
        if not session_record.provider_session_id:
            raise ProviderRestoreError(
                f"cannot restore session {session_record.session_id}: no provider_session_id"
            )
        prior = session_record.provider_session_id
        session_record.attempt += 1
        session_record.last_event_at = _now()
        if prior not in session_record.previous_provider_session_ids:
            session_record.previous_provider_session_ids.append(prior)
        self._sessions[session_record.session_id] = _CodexSessionState(
            mode="restore",
            config=_restored_config(session_record),
        )
        _persist_session(session_record)
        return session_record

    async def send_input(
        self,
        session: SessionRecord,
        message: str,
    ) -> AsyncIterator[AgentEvent]:
        state = self._sessions.get(session.session_id)
        if state is None:
            raise ProviderError(
                f"send_input on unknown session {session.session_id}; "
                "did you call start_session/restore first?"
            )
        if state.closed:
            raise ProviderError(f"send_input on closed session {session.session_id}")

        runner = self._runner or self._run_codex
        result = await runner(session, message, state.config, session.provider_session_id)
        saw_terminal = False
        state.action_denials.clear()

        for line in result.malformed_lines or []:
            yield _envelope(
                event="malformed",
                session=session,
                payload={"reason": "invalid codex jsonl", "line": line},
            )
        if result.malformed_lines:
            yield _envelope(
                event="turn_failed",
                session=session,
                payload={
                    "subtype": "malformed_jsonl",
                    "error": "codex emitted malformed JSONL",
                    "stderr": _diagnostic_text(result.stderr),
                    "last_message": result.last_message,
                },
            )
            session.turn_count += 1
            return

        for denial in _detect_action_denials(result.last_message, source="last_message"):
            state.add_action_denial(denial)

        for raw in result.events:
            for event in _normalize_raw_event(
                raw,
                session=session,
                state=state,
                stderr=result.stderr,
            ):
                if result.returncode != 0 and event.event == "turn_completed":
                    event = AgentEvent(
                        event="turn_failed",
                        timestamp=event.timestamp,
                        session_id=event.session_id,
                        provider=event.provider,
                        issue_identifier=event.issue_identifier,
                        attempt=event.attempt,
                        payload={
                            **event.payload,
                            "subtype": "codex_exit_nonzero",
                            "error": f"codex exited with {result.returncode}",
                            "returncode": result.returncode,
                            "stderr": _diagnostic_text(result.stderr),
                            "last_message": result.last_message,
                        },
                        provider_session_id=event.provider_session_id,
                    )
                yield event
                if event.event in {"turn_completed", "turn_failed", "turn_cancelled"}:
                    saw_terminal = True
                    session.turn_count += 1
                    return

        if result.returncode != 0:
            if not saw_terminal:
                yield _envelope(
                    event="turn_failed",
                    session=session,
                    payload={
                        "returncode": result.returncode,
                        "stderr": _diagnostic_text(result.stderr),
                        "last_message": result.last_message,
                    },
                )
                session.turn_count += 1
            return

        if not saw_terminal:
            yield _envelope(
                event="turn_failed",
                session=session,
                payload={
                    "reason": "codex process exited without terminal event",
                    "stderr": _diagnostic_text(result.stderr),
                    "last_message": result.last_message,
                },
            )
            session.turn_count += 1

    async def interrupt(self, session: SessionRecord) -> AgentEvent:
        return _envelope(
            event="turn_cancelled",
            session=session,
            payload={"subtype": "interrupt", "source": "provider"},
        )

    async def cancel(self, session: SessionRecord) -> AgentEvent:
        state = self._sessions.get(session.session_id)
        if state is not None:
            state.closed = True
        session.terminal_state = Terminal.CANCELLED
        return _envelope(
            event="session_closed",
            session=session,
            payload={"reason": "cancel"},
        )

    async def close(self, session: SessionRecord) -> None:
        state = self._sessions.get(session.session_id)
        if state is not None:
            state.closed = True

    async def _run_codex(
        self,
        session: SessionRecord,
        message: str,
        config: ClaudeConfig,
        provider_session_id: str | None,
    ) -> CodexRunResult:
        session.artifact_dir.mkdir(parents=True, exist_ok=True)
        last_message_path = session.artifact_dir / "codex-last-message.txt"
        raw_events_path = session.artifact_dir / "codex-events.jsonl"
        stderr_path = session.artifact_dir / "codex-stderr.txt"

        cmd = _build_codex_command(
            codex_bin=self._codex_bin,
            config=config,
            workspace=session.workspace_path,
            last_message_path=last_message_path,
            provider_session_id=provider_session_id,
        )
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert proc.stdin is not None
        assert proc.stdout is not None
        assert proc.stderr is not None

        async def read_stderr() -> str:
            data = await proc.stderr.read()
            return data.decode("utf-8", errors="replace")

        stderr_task = asyncio.create_task(read_stderr())
        proc.stdin.write(message.encode("utf-8"))
        await proc.stdin.drain()
        proc.stdin.close()

        events: list[dict[str, Any]] = []
        malformed: list[str] = []
        raw_lines: list[str] = []
        async for raw_line in proc.stdout:
            line = raw_line.decode("utf-8", errors="replace").rstrip("\n")
            raw_lines.append(line)
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                malformed.append(line)
                continue
            if isinstance(item, dict):
                events.append(item)
            else:
                malformed.append(line)

        returncode = await proc.wait()
        stderr = await stderr_task
        raw_payload = "\n".join(raw_lines) + ("\n" if raw_lines else "")
        raw_events_path.write_text(raw_payload, encoding="utf-8")
        if stderr:
            stderr_path.write_text(stderr, encoding="utf-8")
        try:
            last_message = last_message_path.read_text(encoding="utf-8")
        except OSError:
            last_message = None
        return CodexRunResult(
            returncode=returncode,
            events=events,
            stderr=stderr,
            malformed_lines=malformed,
            last_message=last_message,
        )


class _CodexSessionState:
    __slots__ = ("action_denials", "closed", "config", "mode", "saw_first_event")

    def __init__(self, *, config: ClaudeConfig, mode: str = "start_session") -> None:
        self.action_denials: list[dict[str, Any]] = []
        self.closed = False
        self.config = config
        self.mode = mode
        self.saw_first_event = False

    def add_action_denial(self, denial: dict[str, Any]) -> None:
        key = (denial.get("reason"), denial.get("excerpt"))
        existing = {
            (item.get("reason"), item.get("excerpt")) for item in self.action_denials
        }
        if key not in existing:
            self.action_denials.append(denial)


def _build_codex_command(
    *,
    codex_bin: str,
    config: ClaudeConfig,
    workspace: Path,
    last_message_path: Path,
    provider_session_id: str | None,
) -> list[str]:
    cmd = [codex_bin]
    if config.permission_mode == "bypassPermissions":
        cmd.append("--dangerously-bypass-approvals-and-sandbox")
    else:
        cmd.extend(["-a", "never", "--sandbox", "workspace-write"])
    cmd.extend(["-m", config.model, "-C", str(workspace), "exec"])
    if provider_session_id:
        cmd.extend(["resume", "--skip-git-repo-check", provider_session_id])
    else:
        cmd.append("--skip-git-repo-check")
    cmd.extend(["--json", "--output-last-message", str(last_message_path), "-"])
    return cmd


def _restored_config(session: SessionRecord) -> ClaudeConfig:
    """Best-effort config for restore after only a persisted SessionRecord is available.

    The current provider protocol passes config to ``start_session`` only. For
    Codex, enough runtime options can be recovered from the record plus env
    defaults to resume safely; provider-neutral persisted runtime config is
    tracked separately in #276.
    """

    return ClaudeConfig(
        model=os.environ.get("SYMPHONY_CODEX_MODEL", "gpt-5.3-codex"),
        permission_mode=os.environ.get("SYMPHONY_CODEX_PERMISSION_MODE", "acceptEdits"),
        session_store=session.session_store or (session.artifact_dir / "sessions"),
        transcript_store=(
            session.transcript_path.parent if session.transcript_path else session.artifact_dir
        ),
        artifact_store=session.artifact_dir.parent.parent,
    )


def _normalize_raw_event(
    raw: dict[str, Any],
    *,
    session: SessionRecord,
    state: _CodexSessionState,
    stderr: str,
) -> list[AgentEvent]:
    event_type = str(raw.get("type") or "")
    if event_type == "thread.started":
        thread_id = raw.get("thread_id")
        if isinstance(thread_id, str) and thread_id:
            session.provider_session_id = thread_id
            session.last_event_at = _now()
            _persist_session(session)
        if state.saw_first_event:
            return []
        state.saw_first_event = True
        return [
            _envelope(
                event="session_restored" if state.mode == "restore" else "session_started",
                session=session,
                payload={"session_id": session.provider_session_id},
            )
        ]

    if event_type == "turn.started":
        return [_envelope(event="heartbeat", session=session, payload={"kind": event_type})]

    if event_type in {"item.started", "item.completed"}:
        item = raw.get("item") if isinstance(raw.get("item"), dict) else {}
        item_type = str(item.get("type") or "")
        if item_type == "command_execution":
            payload = {
                "tool_name": "command_execution",
                "tool_use_id": item.get("id"),
                "command": item.get("command"),
                "status": item.get("status"),
                "exit_code": item.get("exit_code"),
                "aggregated_output": item.get("aggregated_output"),
            }
            return [
                _envelope(
                    event="tool_started" if event_type == "item.started" else "tool_completed",
                    session=session,
                    payload=payload,
                )
            ]
        if item_type == "agent_message" and event_type == "item.completed":
            text = str(item.get("text") or "")
            for denial in _detect_action_denials(text, source="agent_message"):
                state.add_action_denial(denial)
            return [
                _envelope(event="message_delta", session=session, payload={"text": text}),
                _envelope(event="message_completed", session=session, payload={"text": text}),
            ]
        return [
            _envelope(
                event="heartbeat",
                session=session,
                payload={"kind": event_type, "item": item},
            )
        ]

    if event_type == "turn.completed":
        if not session.provider_session_id:
            return [
                _envelope(
                    event="malformed",
                    session=session,
                    payload={"reason": "missing codex thread.started before terminal event"},
                ),
                _envelope(
                    event="turn_failed",
                    session=session,
                    payload={
                        "subtype": "missing_thread_started",
                        "error": "codex completed without thread.started",
                        "stderr": _diagnostic_text(stderr),
                    },
                ),
            ]
        payload = {"usage": raw.get("usage")}
        if stderr:
            payload["stderr"] = _diagnostic_text(stderr)
        if state.action_denials:
            payload["permission_denials"] = list(state.action_denials)
            payload["codex_warnings"] = {
                "action_denial_count": len(state.action_denials),
                "reason": "codex reported that a requested action was blocked or denied",
            }
        out = []
        if raw.get("usage") is not None:
            out.append(
                _envelope(event="usage", session=session, payload={"usage": raw.get("usage")})
            )
        out.append(_envelope(event="turn_completed", session=session, payload=payload))
        return out

    if event_type in {"turn.failed", "error"}:
        return [
            _envelope(
                event="turn_failed",
                session=session,
                payload={"raw": raw, "stderr": _diagnostic_text(stderr)},
            )
        ]

    return [_envelope(event="malformed", session=session, payload={"raw": raw})]


_ACTION_DENIAL_PATTERNS: tuple[str, ...] = (
    "operation not permitted",
    "permission denied",
    "read-only",
    "read only",
    "write was blocked",
    "blocked by the sandbox",
    "sandbox blocked",
    "couldn't create",
    "couldn’t create",
    "could not create",
    "couldn't write",
    "couldn’t write",
    "could not write",
    "failed to write",
    "not allowed to write",
)


def _detect_action_denials(text: str | None, *, source: str) -> list[dict[str, Any]]:
    if not text:
        return []
    lowered = text.lower()
    matched = [pattern for pattern in _ACTION_DENIAL_PATTERNS if pattern in lowered]
    if not matched:
        return []
    excerpt = " ".join(text.split())
    return [
        {
            "provider": "codex",
            "source": source,
            "reason": "action_denied",
            "matched_patterns": matched,
            "excerpt": excerpt[:500],
        }
    ]


def _diagnostic_text(text: str | None, *, limit: int = 2_000) -> str:
    if not text:
        return ""
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return f"{compact[:limit]}... [truncated; full stderr is in codex-stderr.txt]"


def _envelope(*, event: str, session: SessionRecord, payload: dict[str, Any]) -> AgentEvent:
    return AgentEvent(
        event=event,
        timestamp=_now(),
        session_id=session.session_id,
        provider="codex",
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
            "transcript_path": str(record.transcript_path) if record.transcript_path else None,
            "attempt": record.attempt,
            "turn_count": record.turn_count,
            "started_at": record.started_at.isoformat() if record.started_at else None,
            "last_event_at": record.last_event_at.isoformat() if record.last_event_at else None,
            "terminal_state": record.terminal_state.value if record.terminal_state else None,
            "previous_provider_session_ids": list(record.previous_provider_session_ids),
            "session_store": str(record.session_store),
        }
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(json.dumps(snapshot, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(target)
    except OSError as exc:
        _LOG.warning("could not persist codex session %s: %s", record.session_id, exc)


__all__ = ["CodexProvider", "CodexRunResult"]
