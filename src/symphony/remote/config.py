"""Remote execution configuration model.

Defines the remote execution config schema and validation per M7.4 design.
Remote execution is disabled by default and requires explicit opt-in.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RemoteConfig:
    """Remote worker execution configuration.

    When enabled, the orchestrator dispatches work to remote hosts via SSH.
    Remote execution is disabled by default.

    Fields:
        enabled: Enable remote execution (default: False)
        host: SSH host for remote worker (required when enabled)
        workspace_root: Remote workspace directory (required when enabled)
        artifact_root: Remote artifact directory (required when enabled)
        session_store: Remote session store directory (required when enabled)
        worker_timeout_ms: Maximum worker execution time in milliseconds
        heartbeat_interval_ms: Expected heartbeat interval in milliseconds
        stall_timeout_ms: Maximum time without heartbeat before stall detection
    """

    enabled: bool = False
    host: str | None = None
    workspace_root: str | None = None
    artifact_root: str | None = None
    session_store: str | None = None
    worker_timeout_ms: int = 7_200_000
    heartbeat_interval_ms: int = 30_000
    stall_timeout_ms: int = 300_000
