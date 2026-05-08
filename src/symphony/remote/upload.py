"""Upload materialized remote dispatch payloads to a remote host."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Protocol

from symphony.artifacts import redact_text
from symphony.remote.plan import RemoteDispatchPlan

PAYLOAD_UPLOAD_REDACT_KEYS = ("token", "authorization", "api_key", "password", "secret")


class PayloadUploadRunner(Protocol):
    """Protocol for payload upload subprocess execution."""

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


class RealPayloadUploadRunner:
    """Production runner using subprocess.run."""

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


@dataclass(frozen=True, slots=True)
class PayloadUploadResult:
    """Result of staging remote dispatch payload files."""

    uploaded: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def partial(self) -> bool:
        return bool(self.uploaded and self.errors)


@dataclass(slots=True)
class SCPPayloadUploader:
    """Uploads materialized payload files via scp with fakeable subprocess I/O."""

    host: str
    runner: PayloadUploadRunner
    timeout_seconds: float = 300.0
    redact_keys: tuple[str, ...] = PAYLOAD_UPLOAD_REDACT_KEYS
    extra_secrets: tuple[str, ...] = ()

    def upload(self, plan: RemoteDispatchPlan) -> PayloadUploadResult:
        """Upload the snapshot and dispatch payloads for ``plan``."""
        payloads = (
            ("snapshot", plan.local_snapshot_path, plan.remote_snapshot_path),
            ("dispatch", plan.local_dispatch_path, plan.remote_dispatch_path),
        )
        uploaded: list[str] = []
        errors: list[str] = []

        for name, local_path, remote_path in payloads:
            if not local_path.exists():
                errors.append(f"{name}: local payload file not found: {local_path}")
                continue

            command = ["scp", "-q", str(local_path), f"{self.host}:{remote_path}"]
            try:
                result = self.runner.run(
                    command,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout_seconds,
                )
            except subprocess.TimeoutExpired:
                errors.append(f"{name}: scp timed out after {self.timeout_seconds}s")
                continue
            except OSError as exc:
                errors.append(f"{name}: scp failed: {self._redact(str(exc))}")
                continue

            if result.returncode != 0:
                detail = result.stderr.strip() or f"scp exited with code {result.returncode}"
                errors.append(f"{name}: {self._redact(detail)}")
                continue

            uploaded.append(remote_path)

        return PayloadUploadResult(uploaded=tuple(uploaded), errors=tuple(errors))

    def _redact(self, value: str) -> str:
        return redact_text(
            value,
            redact_keys=self.redact_keys,
            extra_secrets=self.extra_secrets,
        )
