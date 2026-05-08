"""Tests for symphony-worker CLI stub."""

import json
import subprocess
import sys
from pathlib import Path

from symphony.remote.protocol import parse_worker_event


def _minimal_snapshot() -> dict:
    """Return minimal valid config snapshot."""
    return {
        "tracker": {
            "kind": "github",
            "owner": "test-owner",
            "repo": "test-repo",
            "token": "test-token-12345",
        },
        "agent": {"provider": "claude_code"},
        "workspace": {"root": "/tmp/workspaces"},
        "claude": {
            "model": "claude-fake",
            "permission_mode": "acceptEdits",
            "session_store": "/tmp/sessions",
            "transcript_store": "/tmp/transcripts",
            "artifact_store": "/tmp/artifacts",
        },
        "github": {},
        "remote": {
            "enabled": True,
            "host": "user@remote-host",
            "workspace_root": "/remote/workspaces",
            "artifact_root": "/remote/artifacts",
            "session_store": "/remote/sessions",
        },
    }


def test_worker_cli_help():
    """Test symphony-worker --help works."""
    result = subprocess.run(
        [sys.executable, "-m", "symphony.remote.worker", "--help"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "symphony-worker" in result.stdout
    assert "--snapshot-path" in result.stdout
    assert "--fake" in result.stdout


def test_worker_rejects_missing_snapshot(tmp_path: Path):
    """Test worker rejects missing snapshot file."""
    snapshot_path = tmp_path / "missing.json"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "symphony.remote.worker",
            "--snapshot-path",
            str(snapshot_path),
            "--fake",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "not found" in result.stderr.lower()


def test_worker_rejects_malformed_json(tmp_path: Path):
    """Test worker rejects malformed JSON snapshot."""
    snapshot_path = tmp_path / "malformed.json"
    snapshot_path.write_text("{invalid json", encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "symphony.remote.worker",
            "--snapshot-path",
            str(snapshot_path),
            "--fake",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "malformed" in result.stderr.lower() or "json" in result.stderr.lower()


def test_worker_rejects_invalid_snapshot(tmp_path: Path):
    """Test worker rejects snapshot with missing required fields."""
    snapshot_path = tmp_path / "invalid.json"
    snapshot = _minimal_snapshot()
    del snapshot["remote"]["host"]
    snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "symphony.remote.worker",
            "--snapshot-path",
            str(snapshot_path),
            "--fake",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "host" in result.stderr.lower()


def test_worker_rejects_disabled_remote(tmp_path: Path):
    """Test worker rejects snapshot with remote.enabled=false."""
    snapshot_path = tmp_path / "disabled.json"
    snapshot = _minimal_snapshot()
    snapshot["remote"]["enabled"] = False
    snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "symphony.remote.worker",
            "--snapshot-path",
            str(snapshot_path),
            "--fake",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "enabled" in result.stderr.lower()


def test_worker_fake_mode_emits_valid_protocol_events(tmp_path: Path):
    """Test worker --fake emits valid protocol events."""
    snapshot_path = tmp_path / "valid.json"
    snapshot = _minimal_snapshot()
    snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "symphony.remote.worker",
            "--snapshot-path",
            str(snapshot_path),
            "--fake",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0

    # Parse all emitted events
    lines = result.stdout.strip().split("\n")
    assert len(lines) >= 5  # At least 5 events

    events = []
    for line in lines:
        event = parse_worker_event(line)
        events.append(event)

    # Check event sequence
    assert events[0].event == "worker_started"
    assert events[1].event == "workspace_ready"
    assert events[2].event == "session_started"
    assert events[3].event == "heartbeat"
    assert events[4].event == "worker_completed"

    # Check common fields
    for event in events:
        assert event.timestamp
        assert event.issue_identifier
        assert event.attempt >= 1
        assert event.host


def test_worker_error_output_redacts_tokens(tmp_path: Path):
    """Test worker redacts token-looking values from error output."""
    snapshot_path = tmp_path / "with-token.json"
    snapshot = _minimal_snapshot()
    snapshot["tracker"]["token"] = "ghp_secret123456789"
    # Make it invalid to trigger error
    del snapshot["remote"]["host"]
    snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "symphony.remote.worker",
            "--snapshot-path",
            str(snapshot_path),
            "--fake",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    # Token should not appear in stderr
    assert "ghp_secret123456789" not in result.stderr
    assert "secret" not in result.stderr.lower()


def test_worker_has_no_tracker_api_dependency():
    """Test worker module does not import tracker client."""
    # This test verifies the worker can be imported without tracker dependencies
    from symphony.remote import worker

    # Worker should only depend on config and protocol
    assert hasattr(worker, "main")
    assert hasattr(worker, "load_and_validate_snapshot")
    assert hasattr(worker, "run_fake_worker")

    # Verify worker module itself doesn't import tracker
    # (Other tests may have imported it, so we check the module's imports)
    import inspect

    worker_source = inspect.getsource(worker)
    assert "from symphony.github" not in worker_source
    assert "import symphony.github" not in worker_source
