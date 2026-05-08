"""Materialize remote dispatch payload files for coordinator-side planning.

Writes config snapshot and dispatch request files to disk without executing SSH
or uploading to remote hosts. Pure coordinator file I/O.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from symphony.config import WorkflowConfig
from symphony.remote.plan import RemoteDispatchPlan

# Remote workers must not receive the coordinator's tracker API token.
# This placeholder preserves the WorkflowConfig shape until a narrow
# git-only credential is modeled.
REMOTE_TRACKER_TOKEN_PLACEHOLDER = "remote-worker-no-tracker-token"


@dataclass(frozen=True, slots=True)
class MaterializeResult:
    """Result of materializing remote dispatch payload files."""

    snapshot_path: Path
    dispatch_path: Path
    snapshot_bytes: int
    dispatch_bytes: int


def materialize_remote_dispatch_plan(
    plan: RemoteDispatchPlan,
    config: WorkflowConfig,
) -> MaterializeResult:
    """Materialize remote dispatch plan to local payload files.

    Writes config snapshot and dispatch request to the paths specified in the plan.
    Creates parent directories if needed. Overwrites existing files.

    Args:
        plan: Remote dispatch plan with paths and dispatch request
        config: Workflow configuration to serialize

    Returns:
        MaterializeResult with written paths and byte counts

    Raises:
        OSError: If file write fails
    """
    # Create parent directories
    plan.local_snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    plan.local_dispatch_path.parent.mkdir(parents=True, exist_ok=True)

    # Serialize config snapshot (remote-safe, no coordinator tracker token)
    snapshot_json = _serialize_config_snapshot(config)
    snapshot_bytes = plan.local_snapshot_path.write_text(snapshot_json, encoding="utf-8")

    # Serialize dispatch request
    dispatch_json = plan.serialize_dispatch_request()
    dispatch_bytes = plan.local_dispatch_path.write_text(dispatch_json, encoding="utf-8")

    return MaterializeResult(
        snapshot_path=plan.local_snapshot_path,
        dispatch_path=plan.local_dispatch_path,
        snapshot_bytes=snapshot_bytes,
        dispatch_bytes=dispatch_bytes,
    )


def _serialize_config_snapshot(config: WorkflowConfig) -> str:
    """Serialize config to JSON snapshot for remote worker.

    Remote-safe snapshot that excludes coordinator tracker token and includes
    only worker-required config sections.

    Args:
        config: Workflow configuration

    Returns:
        JSON string of config snapshot
    """
    snapshot = {
        "tracker": {
            "kind": config.tracker.kind,
            "owner": config.tracker.owner,
            "repo": config.tracker.repo,
            # Remote workers must not receive the coordinator's tracker API
            # token. The placeholder preserves the current WorkflowConfig
            # shape until a narrow git-only credential is modeled.
            "token": REMOTE_TRACKER_TOKEN_PLACEHOLDER,
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
