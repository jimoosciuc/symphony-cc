"""Tests for SSH remote transport."""

import subprocess
from pathlib import Path

from symphony.config import build_config
from symphony.remote.protocol import WorkerEvent, serialize_worker_event
from symphony.remote.ssh import SSHRemoteTransport


class FakeSubprocessRunner:
    """Fake subprocess runner for deterministic testing."""

    def __init__(
        self,
        *,
        stdout: str = "",
        stderr: str = "",
        returncode: int = 0,
        raise_timeout: bool = False,
        raise_error: Exception | None = None,
    ):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.raise_timeout = raise_timeout
        self.raise_error = raise_error
        self.last_args: list[str] | None = None
        self.last_timeout: float | None = None

    def run(
        self,
        args: list[str],
        *,
        capture_output: bool = True,
        text: bool = True,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess:
        """Fake subprocess.run that returns pre-configured results."""
        self.last_args = args
        self.last_timeout = timeout

        if self.raise_timeout:
            raise subprocess.TimeoutExpired(args, timeout)
        if self.raise_error:
            raise self.raise_error

        return subprocess.CompletedProcess(
            args=args,
            returncode=self.returncode,
            stdout=self.stdout,
            stderr=self.stderr,
        )


def _minimal_config(tmp_path: Path) -> dict:
    """Return minimal valid workflow config with remote enabled."""
    return {
        "tracker": {
            "kind": "github",
            "owner": "test-owner",
            "repo": "test-repo",
            "token": "ghp_test_token_12345",
        },
        "agent": {"provider": "claude_code"},
        "workspace": {"root": str(tmp_path / "workspaces")},
        "claude": {
            "model": "claude-fake",
            "permission_mode": "acceptEdits",
            "session_store": str(tmp_path / "sessions"),
            "transcript_store": str(tmp_path / "transcripts"),
            "artifact_store": str(tmp_path / "artifacts"),
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


def test_ssh_transport_builds_correct_command(tmp_path: Path):
    """Test SSH transport constructs correct SSH command."""
    raw = _minimal_config(tmp_path)
    config = build_config(raw, workflow_path=tmp_path / "WORKFLOW.md")

    runner = FakeSubprocessRunner(stdout="", returncode=0)
    snapshot_path = tmp_path / "snapshot.json"
    transport = SSHRemoteTransport(runner=runner, snapshot_path=snapshot_path)

    transport.run(config)

    assert runner.last_args is not None
    assert runner.last_args[0] == "ssh"
    assert runner.last_args[1] == "user@remote-host"
    assert "symphony-worker" in runner.last_args
    assert "--snapshot-path" in runner.last_args
    assert "--fake" in runner.last_args


def test_ssh_transport_parses_stdout_jsonl_events(tmp_path: Path):
    """Test SSH transport parses JSONL events from stdout."""
    raw = _minimal_config(tmp_path)
    config = build_config(raw, workflow_path=tmp_path / "WORKFLOW.md")

    # Create fake worker events
    events = [
        WorkerEvent(
            event="worker_started",
            timestamp="2026-05-08T12:00:00Z",
            issue_identifier="test/repo#1",
            attempt=1,
            host="remote-host",
            fields={"worker_id": "worker-1"},
        ),
        WorkerEvent(
            event="worker_completed",
            timestamp="2026-05-08T12:01:00Z",
            issue_identifier="test/repo#1",
            attempt=1,
            host="remote-host",
            fields={
                "exit_code": 0,
                "artifact_path": "/remote/artifacts/1",
                "artifacts_ready": True,
            },
        ),
    ]
    stdout = "\n".join(serialize_worker_event(e) for e in events)

    runner = FakeSubprocessRunner(stdout=stdout, returncode=0)
    transport = SSHRemoteTransport(runner=runner, snapshot_path=tmp_path / "snapshot.json")

    result = transport.run(config)

    assert result.ok
    assert len(result.events) == 2
    assert result.events[0].event == "worker_started"
    assert result.events[1].event == "worker_completed"
    assert not result.failed
    assert not result.stalled


def test_ssh_transport_handles_malformed_event_output(tmp_path: Path):
    """Test SSH transport handles malformed JSONL without crashing."""
    raw = _minimal_config(tmp_path)
    config = build_config(raw, workflow_path=tmp_path / "WORKFLOW.md")

    # Mix valid and malformed events
    line1 = (
        '{"event": "worker_started", "timestamp": "2026-05-08T12:00:00Z", '
        '"issue_identifier": "test/repo#1", "attempt": 1, "host": "remote-host", '
        '"worker_id": "worker-1"}'
    )
    line2 = "{invalid json"
    line3 = '{"event": "unknown_event", "timestamp": "2026-05-08T12:00:00Z"}'
    line4 = (
        '{"event": "worker_completed", "timestamp": "2026-05-08T12:01:00Z", '
        '"issue_identifier": "test/repo#1", "attempt": 1, "host": "remote-host", '
        '"exit_code": 0, "artifact_path": "/remote/artifacts/1", "artifacts_ready": true}'
    )
    stdout = f"{line1}\n{line2}\n{line3}\n{line4}"

    runner = FakeSubprocessRunner(stdout=stdout, returncode=0)
    transport = SSHRemoteTransport(runner=runner, snapshot_path=tmp_path / "snapshot.json")

    result = transport.run(config)

    # Should parse valid events and report errors for malformed ones
    assert len(result.events) == 2
    assert result.events[0].event == "worker_started"
    assert result.events[1].event == "worker_completed"
    assert len(result.errors) >= 1  # At least one protocol error
    assert any("Protocol error" in err for err in result.errors)


def test_ssh_transport_captures_non_zero_exit_with_stderr(tmp_path: Path):
    """Test SSH transport captures non-zero exit and stderr."""
    raw = _minimal_config(tmp_path)
    config = build_config(raw, workflow_path=tmp_path / "WORKFLOW.md")

    runner = FakeSubprocessRunner(
        stdout="",
        stderr="SSH connection failed: permission denied",
        returncode=255,
    )
    transport = SSHRemoteTransport(runner=runner, snapshot_path=tmp_path / "snapshot.json")

    result = transport.run(config)

    assert result.failed
    assert len(result.errors) >= 2
    assert any("SSH stderr" in err for err in result.errors)
    assert any("exited with code 255" in err for err in result.errors)


def test_ssh_transport_propagates_worker_failed_event(tmp_path: Path):
    """Test SSH transport propagates worker_failed event."""
    raw = _minimal_config(tmp_path)
    config = build_config(raw, workflow_path=tmp_path / "WORKFLOW.md")

    event = WorkerEvent(
        event="worker_failed",
        timestamp="2026-05-08T12:00:00Z",
        issue_identifier="test/repo#1",
        attempt=1,
        host="remote-host",
        fields={"error_type": "config_error", "message": "Invalid config", "retryable": False},
    )
    stdout = serialize_worker_event(event)

    runner = FakeSubprocessRunner(stdout=stdout, returncode=1)
    transport = SSHRemoteTransport(runner=runner, snapshot_path=tmp_path / "snapshot.json")

    result = transport.run(config)

    assert result.failed
    assert len(result.events) == 1
    assert result.events[0].event == "worker_failed"


def test_ssh_transport_handles_timeout(tmp_path: Path):
    """Test SSH transport handles subprocess timeout."""
    raw = _minimal_config(tmp_path)
    config = build_config(raw, workflow_path=tmp_path / "WORKFLOW.md")

    runner = FakeSubprocessRunner(raise_timeout=True)
    transport = SSHRemoteTransport(runner=runner, snapshot_path=tmp_path / "snapshot.json")

    result = transport.run(config)

    assert result.failed
    assert len(result.errors) == 1
    assert "timed out" in result.errors[0].lower()


def test_ssh_transport_redacts_token_from_stderr(tmp_path: Path):
    """Test SSH transport redacts token-shaped values from stderr."""
    raw = _minimal_config(tmp_path)
    config = build_config(raw, workflow_path=tmp_path / "WORKFLOW.md")

    # Stderr contains the token
    stderr = f"Error: authentication failed with token {config.tracker.token}"

    runner = FakeSubprocessRunner(stdout="", stderr=stderr, returncode=1)
    transport = SSHRemoteTransport(runner=runner, snapshot_path=tmp_path / "snapshot.json")

    result = transport.run(config)

    assert result.failed
    # Token should not appear in errors
    for error in result.errors:
        assert config.tracker.token not in error
        assert "ghp_test_token" not in error


def test_ssh_transport_redacts_configured_snapshot_secrets(tmp_path: Path):
    """Test SSH transport redacts token-shaped values from stderr."""
    raw = _minimal_config(tmp_path)
    config = build_config(raw, workflow_path=tmp_path / "WORKFLOW.md")

    # Stderr contains a token-shaped value (OpenAI-style)
    token_shaped = "sk-proj-abcdefghijklmnopqrstuvwxyz123456"
    stderr = f"Error: API key {token_shaped} is invalid"

    runner = FakeSubprocessRunner(stdout="", stderr=stderr, returncode=1)
    transport = SSHRemoteTransport(runner=runner, snapshot_path=tmp_path / "snapshot.json")

    result = transport.run(config)

    assert result.failed
    # Token-shaped value should be redacted from stderr
    assert token_shaped not in result.errors[0]  # First error is SSH stderr
    assert "<redacted>" in result.errors[0] or "[REDACTED]" in result.errors[0]


def test_ssh_transport_respects_worker_timeout_config(tmp_path: Path):
    """Test SSH transport uses configured worker timeout."""
    raw = _minimal_config(tmp_path)
    raw["remote"]["worker_timeout_ms"] = 60_000  # 60 seconds
    raw["remote"]["heartbeat_interval_ms"] = 10_000  # 10 seconds
    raw["remote"]["stall_timeout_ms"] = 20_000  # 20 seconds (must be > heartbeat and <= worker)
    config = build_config(raw, workflow_path=tmp_path / "WORKFLOW.md")

    runner = FakeSubprocessRunner(stdout="", returncode=0)
    transport = SSHRemoteTransport(runner=runner, snapshot_path=tmp_path / "snapshot.json")

    transport.run(config)

    assert runner.last_timeout == 60.0  # 60 seconds
