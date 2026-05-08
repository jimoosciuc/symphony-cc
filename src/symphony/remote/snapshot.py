"""Remote worker config snapshot serialization.

The coordinator owns the full workflow config and tracker credentials. Remote
workers receive only the config shape needed to run worker-side code, with the
coordinator tracker token replaced by a non-secret placeholder.
"""

from __future__ import annotations

import json
from pathlib import Path

from symphony.config import WorkflowConfig

REMOTE_TRACKER_TOKEN_PLACEHOLDER = "remote-worker-no-tracker-token"


def build_config_snapshot(
    config: WorkflowConfig,
    *,
    workspace_root: str | Path | None = None,
) -> dict:
    """Build a remote-safe workflow config snapshot."""

    snapshot_workspace_root = (
        str(workspace_root) if workspace_root is not None else str(config.workspace.root)
    )
    remote_section = {
        "enabled": config.remote.enabled,
        "host": config.remote.host,
        "workspace_root": config.remote.workspace_root,
        "artifact_root": config.remote.artifact_root,
        "session_store": config.remote.session_store,
        "worker_timeout_ms": config.remote.worker_timeout_ms,
        "heartbeat_interval_ms": config.remote.heartbeat_interval_ms,
        "stall_timeout_ms": config.remote.stall_timeout_ms,
    }
    if config.remote.git_token and config.remote.git_token != config.tracker.token:
        remote_section["git_token"] = config.remote.git_token

    return {
        "tracker": {
            "kind": config.tracker.kind,
            "owner": config.tracker.owner,
            "repo": config.tracker.repo,
            # Remote workers must not receive the coordinator's tracker API
            # token. The placeholder preserves the WorkflowConfig shape until
            # a narrow git-only credential is modeled.
            "token": REMOTE_TRACKER_TOKEN_PLACEHOLDER,
        },
        "agent": {"provider": config.agent.provider},
        "workspace": {"root": snapshot_workspace_root},
        "claude": {
            "model": config.claude.model,
            "permission_mode": config.claude.permission_mode,
            "session_store": str(config.claude.session_store),
            "transcript_store": str(config.claude.transcript_store),
            "artifact_store": str(config.claude.artifact_store),
        },
        "github": {},
        "logging": {
            "redact_keys": tuple(
                dict.fromkeys((*config.logging.redact_keys, "git_token"))
            ),
        },
        "remote": remote_section,
    }


def serialize_config_snapshot(
    config: WorkflowConfig,
    *,
    workspace_root: str | Path | None = None,
) -> str:
    """Serialize a remote-safe workflow config snapshot as JSON."""

    return json.dumps(
        build_config_snapshot(config, workspace_root=workspace_root),
        indent=2,
    )
