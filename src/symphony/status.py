"""Runtime status snapshot API (#55).

This module exposes a read-only in-memory snapshot. It does not start a
web server, write controls, or persist a database; those can be layered
on later by dashboard work.
"""

from __future__ import annotations

import json
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
        "security": {
            "profile": config.security.profile,
            "permission_mode": config.claude.permission_mode,
        },
        "capacity": {
            "active": len(orchestrator.active),
            "max_concurrency": config.agent.max_concurrency,
        },
        "active_workers": [
            _worker_status(worker) for worker in orchestrator.active.values()
        ],
        "waiting_items": [
            _waiting_status(item, config=config)
            for item in getattr(orchestrator, "role_waiting", {}).values()
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
        "lane": worker.lane.name if getattr(worker, "lane", None) else None,
        "role": getattr(worker, "role_name", None),
        "role_state": getattr(worker, "role_state", None),
        "role_actor": getattr(worker, "role_actor", None),
        "gate_owner": getattr(worker, "gate_owner", None),
        "attempt": session.attempt,
        "security_profile": _security_profile(worker),
        "turn_count": worker.turn_count,
        "terminal_state": (
            worker.terminal_state.value if worker.terminal_state else None
        ),
        "retry_state": "active",
        "last_event": _event_summary(event),
        "recent_events": [_event_summary(item) for item in worker.recent_events],
        "error": worker.error,
        "timeout_subtype": worker.timeout_subtype,
        "usage": worker.usage.to_json() if worker.usage.has_usage else None,
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


def _waiting_status(item: Any, *, config: Any) -> dict[str, Any]:
    payload = dict(item) if isinstance(item, dict) else {}
    issue_identifier = payload.get("issue_identifier")
    if not isinstance(issue_identifier, str):
        return payload
    artifact = _latest_issue_artifact(config.claude.artifact_store, issue_identifier)
    if artifact is None:
        return payload
    payload.setdefault("artifact_dir", str(artifact))

    terminal = _read_json_object(artifact / "terminal.json")
    if terminal:
        payload.setdefault("terminal_state", terminal.get("terminal_state"))
        payload.setdefault("task_outcome", terminal.get("task_outcome"))
        payload.setdefault("provider_session_id", terminal.get("provider_session_id"))
        payload.setdefault("attempt", terminal.get("turn_count") or _attempt_from_path(artifact))
        payload.setdefault("last_event_at", terminal.get("last_event_at"))
        payload.setdefault("permission_denials_count", terminal.get("permission_denials_count"))
        payload.setdefault("role_transition", terminal.get("role_transition"))
        payload.setdefault("task_evidence", terminal.get("task_evidence"))

    events = _read_recent_events(artifact / "events.jsonl", limit=40)
    if events:
        payload.setdefault("recent_events", events)
        payload.setdefault("last_event", events[-1])
    return payload


def _latest_issue_artifact(artifact_store: Path, issue_identifier: str) -> Path | None:
    if "#" not in issue_identifier or "/" not in issue_identifier:
        return None
    repo_part, number_part = issue_identifier.rsplit("#", 1)
    owner_repo = repo_part.replace("/", "_")
    issue_dir = Path(artifact_store) / f"{owner_repo}_{number_part}"
    if not issue_dir.exists():
        return None
    attempts = [path for path in issue_dir.iterdir() if path.is_dir() and path.name.isdigit()]
    if not attempts:
        return None
    return max(attempts, key=lambda path: int(path.name))


def _attempt_from_path(path: Path) -> int | None:
    return int(path.name) if path.name.isdigit() else None


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _read_recent_events(path: Path, *, limit: int) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for line in lines[-limit:]:
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            out.append(data)
    return out


def _security_profile(worker: Any) -> str | None:
    config = getattr(worker, "config", None)
    security = getattr(config, "security", None)
    profile = getattr(security, "profile", None)
    return str(profile) if profile is not None else None


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
