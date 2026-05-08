"""Tests for symphony-worker CLI stub."""

import json
import subprocess
import sys
from pathlib import Path

from symphony.artifacts import REDACTED
from symphony.remote.protocol import parse_worker_event
from symphony.remote.worker import _redact_error_message


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

    # Create dispatch request
    dispatch_path = tmp_path / "dispatch.json"
    dispatch_path.write_text(
        json.dumps(
            {
                "owner": "test-owner",
                "repo": "test-repo",
                "issue_number": 42,
                "attempt": 2,
                "workspace_path": "/remote/workspaces/test-owner_test-repo_42",
                "artifact_path": "/remote/artifacts/test-owner_test-repo_42/2",
            }
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "symphony.remote.worker",
            "--snapshot-path",
            str(snapshot_path),
            "--dispatch-path",
            str(dispatch_path),
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

    # Check that events use dispatch request fields
    for event in events:
        assert event.timestamp
        assert event.issue_identifier == "test-owner/test-repo#42"
        assert event.attempt == 2
        assert event.host

    # Check workspace_ready uses dispatch workspace_path
    assert events[1].fields["workspace_path"] == "/remote/workspaces/test-owner_test-repo_42"

    # Check worker_completed uses dispatch artifact_path
    assert events[4].fields["artifact_path"] == "/remote/artifacts/test-owner_test-repo_42/2"


def test_worker_fake_mode_requires_dispatch_path(tmp_path: Path):
    """Test worker --fake requires --dispatch-path."""
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
    assert result.returncode == 1
    assert "dispatch-path required" in result.stderr.lower()


def test_worker_dispatch_request_error_redaction(tmp_path: Path):
    """Test worker redacts secrets from dispatch request errors."""
    snapshot_path = tmp_path / "valid.json"
    snapshot = _minimal_snapshot()
    snapshot["tracker"]["token"] = "ghp_secret_token_12345"
    snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")

    # Create malformed dispatch request
    dispatch_path = tmp_path / "malformed.json"
    dispatch_path.write_text("{invalid json", encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "symphony.remote.worker",
            "--snapshot-path",
            str(snapshot_path),
            "--dispatch-path",
            str(dispatch_path),
            "--fake",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    # Token should not appear in stderr
    assert "ghp_secret_token_12345" not in result.stderr


def test_worker_dispatch_path_error_redacts_token_shaped_path(tmp_path: Path):
    """Test worker redacts token-shaped values from dispatch path errors."""
    snapshot_path = tmp_path / "valid.json"
    snapshot = _minimal_snapshot()
    snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")
    token_shaped = "ghp_abcdefghijklmnopqrstuvwxyz123456"
    dispatch_path = tmp_path / token_shaped

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "symphony.remote.worker",
            "--snapshot-path",
            str(snapshot_path),
            "--dispatch-path",
            str(dispatch_path),
            "--fake",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert token_shaped not in result.stderr
    assert REDACTED in result.stderr


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


def test_worker_error_output_redacts_token_shaped_non_tracker_value(tmp_path: Path):
    """Worker stderr redacts token-shaped values even outside tracker.token."""
    snapshot_path = tmp_path / "token-shaped-permission.json"
    token_shaped_value = "ghp_abcdefghijklmnopqrstuvwxyz123456"
    snapshot = _minimal_snapshot()
    snapshot["claude"]["permission_mode"] = token_shaped_value
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
    assert token_shaped_value not in result.stderr
    assert REDACTED in result.stderr


def test_worker_error_redaction_uses_snapshot_secrets_before_config_exists():
    """Failure-path redaction works before WorkflowConfig is available."""
    raw_snapshot = {"tracker": {"token": "plain-secret-token"}}
    message = "snapshot validation failed for plain-secret-token"
    redacted = _redact_error_message(message, raw_snapshot)
    assert "plain-secret-token" not in redacted
    assert REDACTED in redacted


def test_worker_error_redaction_respects_snapshot_redact_keys():
    """Custom redact keys are honored for free-form worker errors."""
    raw_snapshot = {
        "logging": {"redact_keys": ["private_value"]},
        "remote": {"private_value": "custom-secret-value"},
    }
    message = "remote setup failed with custom-secret-value"
    redacted = _redact_error_message(message, raw_snapshot)
    assert "custom-secret-value" not in redacted
    assert REDACTED in redacted


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
