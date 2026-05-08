"""Symphony remote worker CLI stub.

Validates config snapshots and emits protocol events. This is the command
that SSH will invoke on remote hosts in production. For now, it runs in
fake/no-op mode for testing without real workspace/provider execution.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from symphony.config import WorkflowConfig, build_config
from symphony.remote.protocol import WorkerEvent, serialize_worker_event


def main() -> int:
    """Symphony worker CLI entrypoint.

    Usage:
        symphony-worker --snapshot-path /path/to/snapshot.json

    Returns:
        0 on success, non-zero on failure
    """
    import argparse

    parser = argparse.ArgumentParser(
        prog="symphony-worker",
        description="Remote worker for Symphony orchestrator",
    )
    parser.add_argument(
        "--snapshot-path",
        type=Path,
        required=True,
        help="Path to config snapshot JSON file",
    )
    parser.add_argument(
        "--fake",
        action="store_true",
        help="Run in fake/no-op mode (emit events without real execution)",
    )

    args = parser.parse_args()

    try:
        config = load_and_validate_snapshot(args.snapshot_path)
    except Exception as e:
        # Redact token-looking values from error output
        error_msg = str(e)
        if "token" in error_msg.lower():
            token = config.tracker.token if hasattr(config, "tracker") else ""
            error_msg = error_msg.replace(token, "[REDACTED]")
        print(f"Worker failed: {error_msg}", file=sys.stderr)
        return 1

    if args.fake:
        return run_fake_worker(config)

    # Real worker execution not implemented yet
    print("Real worker execution not implemented", file=sys.stderr)
    return 1


def load_and_validate_snapshot(snapshot_path: Path) -> WorkflowConfig:
    """Load and validate a config snapshot from JSON.

    Args:
        snapshot_path: Path to snapshot JSON file

    Returns:
        Validated WorkflowConfig

    Raises:
        FileNotFoundError: If snapshot file doesn't exist
        ValueError: If snapshot is malformed or invalid
    """
    if not snapshot_path.exists():
        raise FileNotFoundError(f"Snapshot file not found: {snapshot_path}")

    try:
        raw = json.loads(snapshot_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError(f"Malformed JSON in snapshot: {e}") from e

    if not isinstance(raw, dict):
        raise ValueError(f"Snapshot must be a JSON object, got {type(raw).__name__}")

    # Build config using existing validation
    # Use a fake workflow path since snapshot is already resolved
    config = build_config(raw, workflow_path=Path("/fake/WORKFLOW.md"))

    # Defensive remote config validation
    if not config.remote.enabled:
        raise ValueError("remote.enabled must be true in worker config snapshot")

    missing = []
    for key in ("host", "workspace_root", "artifact_root", "session_store"):
        if not getattr(config.remote, key):
            missing.append(key)
    if missing:
        raise ValueError(f"remote config missing required fields: {', '.join(missing)}")

    return config


def run_fake_worker(config: WorkflowConfig) -> int:
    """Run worker in fake/no-op mode.

    Emits valid protocol events to stdout without real execution.
    Useful for testing protocol parsing and event handling.

    Args:
        config: Validated workflow config

    Returns:
        0 on success
    """
    now = datetime.now(timezone.utc).isoformat()
    host = config.remote.host or "fake-host"
    issue_id = "fake/repo#1"
    attempt = 1

    # Emit worker_started
    event = WorkerEvent(
        event="worker_started",
        timestamp=now,
        issue_identifier=issue_id,
        attempt=attempt,
        host=host,
        fields={"worker_id": "fake-worker-1"},
    )
    print(serialize_worker_event(event), flush=True)

    # Emit workspace_ready
    event = WorkerEvent(
        event="workspace_ready",
        timestamp=now,
        issue_identifier=issue_id,
        attempt=attempt,
        host=host,
        fields={"workspace_path": f"{config.remote.workspace_root}/fake_repo_1"},
    )
    print(serialize_worker_event(event), flush=True)

    # Emit session_started
    event = WorkerEvent(
        event="session_started",
        timestamp=now,
        issue_identifier=issue_id,
        attempt=attempt,
        host=host,
        fields={"session_id": "fake-session-1", "provider_session_id": "fake-provider-1"},
    )
    print(serialize_worker_event(event), flush=True)

    # Emit heartbeat
    event = WorkerEvent(
        event="heartbeat",
        timestamp=now,
        issue_identifier=issue_id,
        attempt=attempt,
        host=host,
        fields={"status": "running"},
    )
    print(serialize_worker_event(event), flush=True)

    # Emit worker_completed
    event = WorkerEvent(
        event="worker_completed",
        timestamp=now,
        issue_identifier=issue_id,
        attempt=attempt,
        host=host,
        fields={
            "exit_code": 0,
            "artifact_path": f"{config.remote.artifact_root}/fake_repo_1/1",
            "artifacts_ready": True,
        },
    )
    print(serialize_worker_event(event), flush=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())
