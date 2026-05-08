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
from typing import Any

from symphony.artifacts import redact_text
from symphony.config import WorkflowConfig, build_config
from symphony.remote.dispatch import DispatchRequest, load_dispatch_request
from symphony.remote.protocol import WorkerEvent, serialize_worker_event

DEFAULT_REDACT_KEYS = ("token", "authorization", "api_key", "password", "secret")


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
        "--dispatch-path",
        type=Path,
        help="Path to dispatch request JSON file (required for fake mode)",
    )
    parser.add_argument(
        "--fake",
        action="store_true",
        help="Run in fake/no-op mode (emit events without real execution)",
    )

    args = parser.parse_args()

    raw_snapshot = _load_snapshot_for_redaction(args.snapshot_path)

    try:
        config = load_and_validate_snapshot(args.snapshot_path)
    except Exception as e:
        # Redact error message using shared redaction
        error_msg = _redact_error_message(str(e), raw_snapshot)
        print(f"Worker failed: {error_msg}", file=sys.stderr)
        return 1

    if args.fake:
        # Fake mode requires dispatch request
        if not args.dispatch_path:
            print("Worker failed: --dispatch-path required for --fake mode", file=sys.stderr)
            return 1

        try:
            dispatch = load_dispatch_request(args.dispatch_path)
        except Exception as e:
            error_msg = _redact_error_message(str(e), raw_snapshot)
            print(f"Worker failed: {error_msg}", file=sys.stderr)
            return 1

        return run_fake_worker(config, dispatch)

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


def _load_snapshot_for_redaction(snapshot_path: Path) -> dict[str, Any] | None:
    """Best-effort snapshot load for failure-path redaction."""
    try:
        raw = json.loads(snapshot_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(raw, dict):
        return None
    return raw


def _redact_error_message(message: str, raw_snapshot: dict[str, Any] | None) -> str:
    redact_keys = _snapshot_redact_keys(raw_snapshot)
    return redact_text(
        message,
        redact_keys=redact_keys,
        extra_secrets=tuple(_snapshot_secret_values(raw_snapshot, redact_keys=redact_keys)),
    )


def _snapshot_redact_keys(raw_snapshot: dict[str, Any] | None) -> tuple[str, ...]:
    keys = list(DEFAULT_REDACT_KEYS)
    logging_section = raw_snapshot.get("logging") if isinstance(raw_snapshot, dict) else None
    configured = logging_section.get("redact_keys") if isinstance(logging_section, dict) else None
    if isinstance(configured, list):
        keys.extend(key for key in configured if isinstance(key, str))
    return tuple(dict.fromkeys(keys))


def _snapshot_secret_values(value: Any, *, redact_keys: tuple[str, ...]) -> list[str]:
    deny = frozenset(key.lower() for key in redact_keys)
    found: list[str] = []

    def visit(node: Any, *, secret_context: bool = False) -> None:
        if isinstance(node, dict):
            for key, child in node.items():
                child_is_secret = (
                    secret_context or isinstance(key, str) and key.lower() in deny
                )
                visit(child, secret_context=child_is_secret)
            return
        if isinstance(node, list):
            for child in node:
                visit(child, secret_context=secret_context)
            return
        if secret_context and isinstance(node, str) and node:
            found.append(node)

    visit(value)
    return found


def run_fake_worker(config: WorkflowConfig, dispatch: DispatchRequest) -> int:
    """Run worker in fake/no-op mode.

    Emits valid protocol events to stdout without real execution.
    Useful for testing protocol parsing and event handling.

    Args:
        config: Validated workflow config
        dispatch: Dispatch request with issue/workspace/artifact info

    Returns:
        0 on success
    """
    now = datetime.now(timezone.utc).isoformat()
    host = config.remote.host or "fake-host"
    issue_id = dispatch.issue_identifier
    attempt = dispatch.attempt

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
        fields={"workspace_path": dispatch.workspace_path},
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
            "artifact_path": dispatch.artifact_path,
            "artifacts_ready": True,
        },
    )
    print(serialize_worker_event(event), flush=True)

    return 0

if __name__ == "__main__":
    sys.exit(main())
