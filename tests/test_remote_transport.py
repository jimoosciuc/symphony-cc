"""Tests for fake remote worker transport."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from symphony.config import WorkflowConfig, build_config
from symphony.remote.protocol import WorkerEvent, serialize_worker_event
from symphony.remote.transport import (
    FakeRemoteTransport,
    FakeRemoteWorker,
    validate_remote_worker_config,
)


def _config(*, enabled: bool = True) -> WorkflowConfig:
    return build_config(
        {
            "tracker": {
                "kind": "github",
                "owner": "jimoosciuc",
                "repo": "symphony-cc",
                "token": "ghp_abcdefghijklmnopqrstuvwxyz123456",
            },
            "agent": {"provider": "claude_code"},
            "workspace": {"root": "/tmp/ws"},
            "claude": {
                "model": "claude-opus-4-7",
                "permission_mode": "acceptEdits",
                "session_store": "/tmp/sessions",
                "transcript_store": "/tmp/transcripts",
                "artifact_store": "/tmp/artifacts",
            },
            "github": {},
            "remote": {
                "enabled": enabled,
                "host": "builder-1",
                "workspace_root": "/srv/ws",
                "artifact_root": "/srv/artifacts",
                "session_store": "/srv/sessions",
            },
        },
        workflow_path=Path("/tmp/WORKFLOW.md"),
    )


def _line(event: str, **fields: Any) -> str:
    required = {
        "worker_started": {"worker_id": "worker-1"},
        "heartbeat": {"status": "running"},
        "worker_completed": {
            "exit_code": 0,
            "artifact_path": "/srv/artifacts/repo_117/1",
            "artifacts_ready": True,
        },
        "worker_failed": {
            "error_type": "workspace",
            "message": "workspace failed",
            "retryable": False,
        },
    }[event]
    required.update(fields)
    return serialize_worker_event(
        WorkerEvent(
            event=event,
            timestamp="2026-05-08T12:00:00Z",
            issue_identifier="jimoosciuc/symphony-cc#117",
            attempt=1,
            host="builder-1",
            fields=required,
        )
    )


def test_fake_transport_success_streams_protocol_events() -> None:
    transport = FakeRemoteTransport(
        FakeRemoteWorker(
            (
                _line("worker_started"),
                _line("heartbeat"),
                _line("worker_completed"),
            )
        )
    )
    result = transport.run(_config())
    assert result.ok is True
    assert [event.event for event in result.events] == [
        "worker_started",
        "heartbeat",
        "worker_completed",
    ]


def test_fake_transport_reports_worker_failure() -> None:
    transport = FakeRemoteTransport(FakeRemoteWorker((_line("worker_failed"),)))
    result = transport.run(_config())
    assert result.ok is False
    assert result.failed is True
    assert result.events[0].event == "worker_failed"


@pytest.mark.parametrize(
    "line",
    [
        "{not json",
        json.dumps({"event": "heartbeat"}),
        json.dumps(
            {
                "event": "unknown",
                "timestamp": "2026-05-08T12:00:00Z",
                "issue_identifier": "jimoosciuc/symphony-cc#117",
                "attempt": 1,
                "host": "builder-1",
            }
        ),
    ],
)
def test_fake_transport_reports_protocol_errors(line: str) -> None:
    result = FakeRemoteTransport(FakeRemoteWorker((line,))).run(_config())
    assert result.ok is False
    assert result.errors


def test_fake_transport_can_surface_stalled_heartbeat_input() -> None:
    transport = FakeRemoteTransport(
        FakeRemoteWorker((_line("heartbeat"),)),
        heartbeat_deadline_missed=True,
    )
    result = transport.run(_config())
    assert result.ok is False
    assert result.stalled is True


def test_fake_worker_validates_remote_config_snapshot() -> None:
    worker = FakeRemoteWorker((_line("heartbeat"),))
    with pytest.raises(ValueError, match="remote.enabled"):
        worker.run(_config(enabled=False))


def test_fake_worker_has_no_tracker_api_dependency() -> None:
    worker = FakeRemoteWorker((_line("heartbeat"),))
    assert "tracker" not in worker.__dataclass_fields__
    assert validate_remote_worker_config(_config()) is None
