"""Runtime status snapshot API (#55).

This module exposes a read-only in-memory snapshot. It does not start a
web server, write controls, or persist a database; those can be layered
on later by dashboard work.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from symphony.artifacts import redact


def build_status_snapshot(orchestrator: Any) -> dict[str, Any]:
    """Return an operator-readable snapshot of the current daemon state."""

    config = orchestrator.config
    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "run_id": orchestrator.run_id,
        "state": _state(orchestrator),
        "workflow": _workflow_status(orchestrator),
        "capacity": {
            "active": len(orchestrator.active),
            "max_concurrency": config.agent.max_concurrency,
        },
        "active_workers": [
            _worker_status(worker) for worker in orchestrator.active.values()
        ],
        "retry_queue": [
            _retry_status(retry) for retry in orchestrator.retry_states.values()
        ],
        "recent_finished": list(getattr(orchestrator, "recent_finished", [])),
        "recovery_decisions": [
            decision.to_json()
            for decision in getattr(orchestrator, "recovery_decisions", [])
        ],
    }
    return redact(payload, redact_keys=config.logging.redact_keys)


def _state(orchestrator: Any) -> str:
    if orchestrator.active:
        return "running"
    if orchestrator.retry_states:
        return "retry_waiting"
    return "idle"


def _workflow_status(orchestrator: Any) -> dict[str, Any]:
    reloader = getattr(orchestrator, "_workflow_reloader", None)
    snapshot = getattr(reloader, "snapshot", None)
    if snapshot is None:
        return {"revision": None, "path": None, "loaded_at": None}
    return {
        "revision": snapshot.revision,
        "path": str(snapshot.workflow_path) if snapshot.workflow_path else None,
        "loaded_at": snapshot.loaded_at.isoformat(),
    }


def _worker_status(worker: Any) -> dict[str, Any]:
    event = worker.last_event
    session = worker.session
    return {
        "issue_identifier": worker.issue.identifier,
        "issue_url": worker.issue.url,
        "issue_title": worker.issue.title,
        "workspace_path": str(worker.workspace.path),
        "artifact_dir": str(worker.artifacts.root),
        "session_id": session.session_id,
        "provider_session_id": session.provider_session_id,
        "attempt": session.attempt,
        "turn_count": worker.turn_count,
        "terminal_state": (
            worker.terminal_state.value if worker.terminal_state else None
        ),
        "retry_state": "active",
        "last_event": _event_summary(event),
        "error": worker.error,
        "timeout_subtype": worker.timeout_subtype,
    }


def _retry_status(retry: Any) -> dict[str, Any]:
    return {
        "issue_identifier": retry.issue_identifier,
        "attempts": retry.attempts,
        "last_error": retry.last_error,
        "last_attempt_at": _iso(retry.last_attempt_at),
        "next_attempt_at": _iso(retry.next_attempt_at),
        "history": list(retry.history),
    }


def _event_summary(event: Any) -> dict[str, Any] | None:
    if event is None:
        return None
    payload = event.payload or {}
    return {
        "event": event.event,
        "timestamp": event.timestamp.isoformat(),
        "provider_session_id": event.provider_session_id,
        "payload": payload,
    }


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def path_for_display(path: Path | None) -> str | None:
    """Tiny helper for future dashboard code."""
    return str(path) if path is not None else None


__all__ = ["build_status_snapshot", "path_for_display"]
