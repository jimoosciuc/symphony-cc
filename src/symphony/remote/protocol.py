"""JSONL protocol for remote worker status events."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from symphony.artifacts import redact

REMOTE_WORKER_EVENTS: frozenset[str] = frozenset(
    {
        "worker_started",
        "workspace_ready",
        "session_started",
        "heartbeat",
        "turn_completed",
        "worker_completed",
        "worker_failed",
    }
)

COMMON_FIELDS: tuple[str, ...] = (
    "event",
    "timestamp",
    "issue_identifier",
    "attempt",
    "host",
)

EVENT_FIELDS: dict[str, tuple[str, ...]] = {
    "worker_started": ("worker_id",),
    "workspace_ready": ("workspace_path",),
    "session_started": ("session_id", "provider_session_id"),
    "heartbeat": ("status",),
    "turn_completed": ("terminal",),
    "worker_completed": ("exit_code", "artifact_path", "artifacts_ready"),
    "worker_failed": ("error_type", "message", "retryable"),
}


class ProtocolError(ValueError):
    """Raised when a remote worker JSONL event violates the protocol."""


@dataclass(frozen=True, slots=True)
class WorkerEvent:
    """Typed remote worker protocol event.

    Event-specific fields live in ``fields`` and are flattened when serialized
    so each JSONL line is one plain JSON object.
    """

    event: str
    timestamp: str
    issue_identifier: str
    attempt: int
    host: str
    fields: dict[str, Any] = field(default_factory=dict)

    def to_record(self) -> dict[str, Any]:
        record: dict[str, Any] = {
            "event": self.event,
            "timestamp": self.timestamp,
            "issue_identifier": self.issue_identifier,
            "attempt": self.attempt,
            "host": self.host,
        }
        record.update(self.fields)
        return record


def parse_worker_event(line: str) -> WorkerEvent:
    """Parse one worker JSONL line into a typed event."""

    try:
        raw = json.loads(line)
    except json.JSONDecodeError as exc:
        raise ProtocolError(f"malformed JSON: {exc}") from exc

    if not isinstance(raw, dict):
        raise ProtocolError(f"event line must be a JSON object, got {type(raw).__name__}")

    _validate_required(raw, COMMON_FIELDS)
    event_name = raw["event"]
    if not isinstance(event_name, str):
        raise ProtocolError("event must be a string")
    if event_name not in REMOTE_WORKER_EVENTS:
        raise ProtocolError(f"unknown remote worker event: {event_name}")

    _validate_required(raw, EVENT_FIELDS[event_name])

    attempt = raw["attempt"]
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
        raise ProtocolError("attempt must be a positive integer")

    fields = {
        key: value
        for key, value in raw.items()
        if key not in COMMON_FIELDS
    }
    return WorkerEvent(
        event=event_name,
        timestamp=_require_str(raw, "timestamp"),
        issue_identifier=_require_str(raw, "issue_identifier"),
        attempt=attempt,
        host=_require_str(raw, "host"),
        fields=fields,
    )


def serialize_worker_event(
    event: WorkerEvent,
    *,
    redact_keys: tuple[str, ...] = (),
) -> str:
    """Serialize an event to a redacted JSON object string."""

    record = event.to_record()
    _validate_required(record, COMMON_FIELDS)
    if event.event not in REMOTE_WORKER_EVENTS:
        raise ProtocolError(f"unknown remote worker event: {event.event}")
    _validate_required(record, EVENT_FIELDS[event.event])
    if isinstance(event.attempt, bool) or not isinstance(event.attempt, int) or event.attempt < 1:
        raise ProtocolError("attempt must be a positive integer")
    redacted = redact(record, redact_keys=redact_keys)
    return json.dumps(redacted, sort_keys=True)


def _validate_required(record: dict[str, Any], fields: tuple[str, ...]) -> None:
    for key in fields:
        if key not in record:
            raise ProtocolError(f"missing required field: {key}")
        if record[key] is None:
            raise ProtocolError(f"missing required field: {key}")


def _require_str(record: dict[str, Any], key: str) -> str:
    value = record[key]
    if not isinstance(value, str) or not value:
        raise ProtocolError(f"{key} must be a non-empty string")
    return value
