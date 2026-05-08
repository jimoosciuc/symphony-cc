"""SSH-backed remote worker transport.

Executes symphony-worker on remote hosts via SSH and streams protocol events.
Uses subprocess for SSH command execution with fake runner support for testing.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from symphony.artifacts import redact_text
from symphony.config import WorkflowConfig
from symphony.remote.protocol import ProtocolError, WorkerEvent, parse_worker_event
from symphony.remote.transport import RemoteRunResult

# Keys to redact from SSH stderr and errors
SSH_REDACT_KEYS = ("token", "authorization", "api_key", "password", "secret")


class SubprocessRunner(Protocol):
    """Protocol for subprocess execution (allows fake runner in tests)."""

    def run(
        self,
        args: list[str],
        *,
        capture_output: bool = True,
        text: bool = True,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess:
        """Run a subprocess command and return the result."""
        ...


class RealSubprocessRunner:
    """Production subprocess runner using subprocess.run."""

    def run(
        self,
        args: list[str],
        *,
        capture_output: bool = True,
        text: bool = True,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess:
        """Run a subprocess command and return the result."""
        return subprocess.run(
            args,
            capture_output=capture_output,
            text=text,
            timeout=timeout,
            check=False,
        )


@dataclass(slots=True)
class SSHRemoteTransport:
    """SSH-backed transport for remote worker execution.

    Executes symphony-worker on a remote host via SSH, streams stdout JSONL
    events, and captures stderr. Uses a configurable subprocess runner to
    enable deterministic testing without real SSH.
    """

    runner: SubprocessRunner
    snapshot_path: Path | None = None

    def run(self, config: WorkflowConfig) -> RemoteRunResult:
        """Execute remote worker via SSH and parse protocol events.

        Args:
            config: Workflow configuration with remote settings

        Returns:
            RemoteRunResult with parsed events, errors, and failure state
        """
        # Build SSH command
        ssh_args = self._build_ssh_command(config)

        # Execute SSH command
        try:
            timeout_seconds = config.remote.worker_timeout_ms / 1000.0
            result = self.runner.run(ssh_args, timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            return RemoteRunResult(
                errors=(f"SSH command timed out after {timeout_seconds}s",),
                failed=True,
            )
        except Exception as e:
            error_msg = self._redact_error(str(e), config)
            return RemoteRunResult(
                errors=(f"SSH transport error: {error_msg}",),
                failed=True,
            )

        # Parse stdout JSONL events
        events: list[WorkerEvent] = []
        errors: list[str] = []
        failed = False

        for line in result.stdout.strip().split("\n") if result.stdout else []:
            if not line.strip():
                continue
            try:
                event = parse_worker_event(line)
                events.append(event)
                if event.event == "worker_failed":
                    failed = True
            except ProtocolError as exc:
                errors.append(f"Protocol error: {exc}")

        # Capture stderr if present
        if result.stderr:
            redacted_stderr = self._redact_error(result.stderr, config)
            if redacted_stderr.strip():
                errors.append(f"SSH stderr: {redacted_stderr}")

        # Non-zero exit without worker_failed event is a transport failure
        if result.returncode != 0 and not failed:
            errors.append(f"SSH command exited with code {result.returncode}")
            failed = True

        return RemoteRunResult(
            events=tuple(events),
            errors=tuple(errors),
            failed=failed,
            stalled=False,  # Heartbeat stall detection happens at orchestrator level
        )

    def _build_ssh_command(self, config: WorkflowConfig) -> list[str]:
        """Build SSH command to execute symphony-worker on remote host.

        Args:
            config: Workflow configuration with remote settings

        Returns:
            SSH command arguments list
        """
        if not config.remote.host:
            raise ValueError("remote.host is required for SSH transport")

        # Serialize config snapshot to JSON
        snapshot_json = self._serialize_config_snapshot(config)

        # Write snapshot to temp file if snapshot_path is set
        if self.snapshot_path:
            self.snapshot_path.write_text(snapshot_json, encoding="utf-8")
            remote_snapshot_path = str(self.snapshot_path)
        else:
            # For production, we'd use a temp file or stdin
            # For now, assume snapshot is pre-written
            remote_snapshot_path = "/tmp/symphony-snapshot.json"

        # Build SSH command
        ssh_args = [
            "ssh",
            config.remote.host,
            "symphony-worker",
            "--snapshot-path",
            remote_snapshot_path,
            "--fake",  # For now, always use fake mode
        ]

        return ssh_args

    def _serialize_config_snapshot(self, config: WorkflowConfig) -> str:
        """Serialize config to JSON snapshot for remote worker.

        Args:
            config: Workflow configuration

        Returns:
            JSON string of config snapshot
        """
        # Convert config to dict representation
        # This is a simplified version - production would need full serialization
        snapshot = {
            "tracker": {
                "kind": config.tracker.kind,
                "owner": config.tracker.owner,
                "repo": config.tracker.repo,
                "token": config.tracker.token,
            },
            "agent": {"provider": config.agent.provider},
            "workspace": {"root": str(config.workspace.root)},
            "claude": {
                "model": config.claude.model,
                "permission_mode": config.claude.permission_mode,
                "session_store": str(config.claude.session_store),
                "transcript_store": str(config.claude.transcript_store),
                "artifact_store": str(config.claude.artifact_store),
            },
            "github": {},
            "remote": {
                "enabled": config.remote.enabled,
                "host": config.remote.host,
                "workspace_root": config.remote.workspace_root,
                "artifact_root": config.remote.artifact_root,
                "session_store": config.remote.session_store,
                "worker_timeout_ms": config.remote.worker_timeout_ms,
                "heartbeat_interval_ms": config.remote.heartbeat_interval_ms,
                "stall_timeout_ms": config.remote.stall_timeout_ms,
            },
        }
        return json.dumps(snapshot, indent=2)

    def _redact_error(self, error_msg: str, config: WorkflowConfig) -> str:
        """Redact secrets from error messages.

        Args:
            error_msg: Error message to redact
            config: Workflow configuration (for extracting secrets)

        Returns:
            Redacted error message
        """
        # Extract secret values from config
        secrets = [config.tracker.token]

        # Extract any additional secret-like fields from tracker
        if hasattr(config.tracker, 'api_key'):
            secrets.append(config.tracker.api_key)
        if hasattr(config.tracker, 'password'):
            secrets.append(config.tracker.password)

        return redact_text(
            error_msg,
            redact_keys=SSH_REDACT_KEYS,
            extra_secrets=tuple(s for s in secrets if s),
        )
