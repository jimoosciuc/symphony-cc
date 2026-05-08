"""Tests for remote worker JSONL protocol events."""

from __future__ import annotations

import json
from typing import Any

import pytest

from symphony.artifacts import REDACTED
from symphony.remote.protocol import (
    EVENT_FIELDS,
    REMOTE_WORKER_EVENTS,
    ProtocolError,
    WorkerEvent,
    parse_worker_event,
    serialize_worker_event,
)


def _event(event: str, **fields: Any) -> WorkerEvent:
    payload = dict(_required_fields(event))
    payload.update(fields)
    return WorkerEvent(
        event=event,
        timestamp="2026-05-08T12:00:00Z",
        issue_identifier="jimoosciuc/symphony-cc#115",
        attempt=1,
        host="builder-1",
        fields=payload,
    )


def _required_fields(event: str) -> dict[str, Any]:
    return {
        "worker_started": {"worker_id": "worker-1"},
        "workspace_ready": {"workspace_path": "/srv/ws/repo_115"},
        "session_started": {
            "session_id": "symphony-session",
            "provider_session_id": "claude-session",
        },
        "heartbeat": {"status": "running"},
        "turn_completed": {"terminal": {"event": "turn_completed", "status": "completed"}},
        "worker_completed": {
            "exit_code": 0,
            "artifact_path": "/srv/artifacts/repo_115/1",
            "artifacts_ready": True,
        },
        "worker_failed": {
            "error_type": "workspace",
            "message": "workspace failed",
            "retryable": True,
        },
    }[event]


@pytest.mark.parametrize("event_name", sorted(REMOTE_WORKER_EVENTS))
def test_protocol_events_roundtrip(event_name: str) -> None:
    original = _event(event_name)
    line = serialize_worker_event(original)
    parsed = parse_worker_event(line)
    assert parsed == original
    record = json.loads(line)
    assert record["event"] == event_name
    assert record["issue_identifier"] == "jimoosciuc/symphony-cc#115"
    assert record["attempt"] == 1
    assert record["host"] == "builder-1"


def test_parse_rejects_malformed_json() -> None:
    with pytest.raises(ProtocolError, match="malformed JSON"):
        parse_worker_event("{not json")


def test_parse_rejects_non_object_json() -> None:
    with pytest.raises(ProtocolError, match="JSON object"):
        parse_worker_event('"worker_started"')


@pytest.mark.parametrize(
    "field",
    ["event", "timestamp", "issue_identifier", "attempt", "host"],
)
def test_parse_rejects_missing_common_fields(field: str) -> None:
    record = _event("heartbeat").to_record()
    del record[field]
    with pytest.raises(ProtocolError, match=f"missing required field: {field}"):
        parse_worker_event(json.dumps(record))


@pytest.mark.parametrize("event_name", sorted(REMOTE_WORKER_EVENTS))
def test_parse_rejects_missing_event_specific_fields(event_name: str) -> None:
    record = _event(event_name).to_record()
    missing = EVENT_FIELDS[event_name][0]
    del record[missing]
    with pytest.raises(ProtocolError, match=f"missing required field: {missing}"):
        parse_worker_event(json.dumps(record))


def test_parse_rejects_unknown_event_name() -> None:
    record = _event("heartbeat").to_record()
    record["event"] = "message_delta"
    with pytest.raises(ProtocolError, match="unknown remote worker event"):
        parse_worker_event(json.dumps(record))


@pytest.mark.parametrize("attempt", [0, -1, True, "1"])
def test_parse_rejects_invalid_attempt(attempt: Any) -> None:
    record = _event("heartbeat").to_record()
    record["attempt"] = attempt
    with pytest.raises(ProtocolError, match="attempt must be a positive integer"):
        parse_worker_event(json.dumps(record))


def test_serialize_redacts_token_values() -> None:
    event = _event(
        "turn_completed",
        terminal={
            "event": "turn_completed",
            "status": "completed",
            "token": "ghp_abcdefghijklmnopqrstuvwxyz123456",
            "text": "used sk-abcdefghijklmnopqrstuvwxyz123456",
        },
    )
    line = serialize_worker_event(event, redact_keys=("token",))
    assert "ghp_abcdefghijklmnopqrstuvwxyz123456" not in line
    assert "sk-abcdefghijklmnopqrstuvwxyz123456" not in line
    record = json.loads(line)
    assert record["terminal"]["token"] == REDACTED
    assert REDACTED in record["terminal"]["text"]


def test_serialize_rejects_unknown_event_name() -> None:
    event = _event("heartbeat")
    bad = WorkerEvent(
        event="raw_sdk_event",
        timestamp=event.timestamp,
        issue_identifier=event.issue_identifier,
        attempt=event.attempt,
        host=event.host,
        fields={"status": "running"},
    )
    with pytest.raises(ProtocolError, match="unknown remote worker event"):
        serialize_worker_event(bad)


def test_raw_provider_shape_is_not_a_worker_event() -> None:
    raw_provider_event = {
        "type": "message_delta",
        "delta": {"text": "hello"},
        "session_id": "claude-session",
    }
    with pytest.raises(ProtocolError, match="missing required field: event"):
        parse_worker_event(json.dumps(raw_provider_event))
