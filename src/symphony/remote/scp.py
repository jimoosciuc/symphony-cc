"""SSH-backed artifact collection for remote worker execution.

Copies artifacts from remote hosts via scp with coordinator-side redaction.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from symphony.artifacts import redact_text
from symphony.remote.artifacts import ArtifactCollectionResult, _copy_redacted

# Keys to redact from scp stderr and errors
SCP_REDACT_KEYS = ("token", "authorization", "api_key", "password", "secret")


class ScpRunner(Protocol):
    """Protocol for scp subprocess execution (allows fake runner in tests)."""

    def run(
        self,
        args: list[str],
        *,
        capture_output: bool = True,
        text: bool = True,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess:
        """Run an scp command and return the result."""
        ...


class RealScpRunner:
    """Production scp runner using subprocess.run."""

    def run(
        self,
        args: list[str],
        *,
        capture_output: bool = True,
        text: bool = True,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess:
        """Run an scp command and return the result."""
        return subprocess.run(
            args,
            capture_output=capture_output,
            text=text,
            timeout=timeout,
            check=False,
        )


@dataclass(slots=True)
class SSHArtifactCollector:
    """Copies remote artifacts via scp with coordinator-side redaction.

    Uses a configurable scp runner to enable deterministic testing without
    real SSH. Applies coordinator-side redaction as defense in depth.
    """

    artifact_store: Path
    redact_keys: tuple[str, ...]
    runner: ScpRunner
    host: str
    required_files: tuple[str, ...] = (
        "events.jsonl",
        "request.json",
        "session.json",
        "terminal.json",
    )
    timeout_seconds: float = 300.0

    def collect(
        self,
        remote_root: str,
        *,
        owner: str,
        repo: str,
        issue_number: int,
        attempt: int,
    ) -> ArtifactCollectionResult:
        """Collect artifacts from remote host via scp.

        Args:
            remote_root: Remote artifact directory path
            owner: Repository owner
            repo: Repository name
            issue_number: Issue number
            attempt: Attempt number

        Returns:
            ArtifactCollectionResult with copied files, missing files, and errors
        """
        local_root = self.artifact_store / f"{owner}_{repo}_{issue_number}" / str(attempt)
        local_root.mkdir(parents=True, exist_ok=True)

        # Create temp directory for scp downloads before redaction
        temp_root = local_root / ".temp"
        temp_root.mkdir(exist_ok=True)

        copied: list[str] = []
        missing: list[str] = []
        errors: list[str] = []

        for name in self.required_files:
            remote_path = f"{self.host}:{remote_root}/{name}"
            temp_target = temp_root / name
            final_target = local_root / name

            # Try to copy file via scp
            scp_result = self._scp_file(remote_path, temp_target)

            if scp_result.missing:
                missing.append(name)
                continue

            if scp_result.error:
                errors.append(f"{name}: {scp_result.error}")
                continue

            # Apply coordinator-side redaction before final write
            try:
                _copy_redacted(temp_target, final_target, redact_keys=self.redact_keys)
                copied.append(name)
            except Exception as exc:
                errors.append(f"{name}: redaction failed: {exc}")

        # Clean up temp directory
        try:
            for temp_file in temp_root.iterdir():
                temp_file.unlink()
            temp_root.rmdir()
        except Exception:
            pass  # Best effort cleanup

        return ArtifactCollectionResult(
            local_root=local_root,
            copied=tuple(copied),
            missing=tuple(missing),
            errors=tuple(errors),
        )

    def _scp_file(self, remote_path: str, local_target: Path) -> _ScpResult:
        """Copy a single file via scp.

        Args:
            remote_path: Remote file path (host:path format)
            local_target: Local target path

        Returns:
            _ScpResult with success/missing/error status
        """
        scp_args = ["scp", "-q", remote_path, str(local_target)]

        try:
            result = self.runner.run(scp_args, timeout=self.timeout_seconds)
        except subprocess.TimeoutExpired:
            return _ScpResult(error=f"scp timed out after {self.timeout_seconds}s")
        except Exception as exc:
            error_msg = self._redact_error(str(exc))
            return _ScpResult(error=f"scp failed: {error_msg}")

        # Exit code 0 = success
        if result.returncode == 0:
            return _ScpResult(success=True)

        # Check stderr for "No such file" to classify as missing
        stderr = result.stderr or ""
        if "No such file" in stderr or "not found" in stderr.lower():
            return _ScpResult(missing=True)

        # Other non-zero exit = error
        redacted_stderr = self._redact_error(stderr)
        return _ScpResult(error=f"scp exit {result.returncode}: {redacted_stderr}")

    def _redact_error(self, error_msg: str) -> str:
        """Redact secrets from error messages.

        Args:
            error_msg: Error message to redact

        Returns:
            Redacted error message
        """
        return redact_text(
            error_msg,
            redact_keys=SCP_REDACT_KEYS,
            extra_secrets=(),  # No config secrets available at this layer
        )


@dataclass(frozen=True, slots=True)
class _ScpResult:
    """Internal result of a single scp operation."""

    success: bool = False
    missing: bool = False
    error: str | None = None
