"""Symphony remote worker CLI stub.

Validates config snapshots and emits protocol events. This is the command
that SSH will invoke on remote hosts in production. For now, it runs in
fake/no-op mode for testing without real workspace/provider execution.
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from symphony.artifacts import ArtifactWriter, redact_text
from symphony.config import WorkflowConfig, build_config
from symphony.events import TERMINAL_TURN_EVENTS, AgentEvent
from symphony.models import Issue, Workspace
from symphony.provider.base import AgentProviderProtocol, SessionRecord, Terminal
from symphony.remote.dispatch import DispatchRequest, load_dispatch_request
from symphony.remote.protocol import WorkerEvent, serialize_worker_event
from symphony.workspace import WorkspaceManager, WorkspacePopulator

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

    if not args.dispatch_path:
        print("Worker failed: --dispatch-path required", file=sys.stderr)
        return 1

    try:
        dispatch = load_dispatch_request(args.dispatch_path)
    except Exception as e:
        error_msg = _redact_error_message(str(e), raw_snapshot)
        print(f"Worker failed: {error_msg}", file=sys.stderr)
        return 1

    if args.fake:
        return run_fake_worker(config, dispatch)

    return asyncio.run(run_real_worker(config, dispatch))


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


ProviderFactory = Callable[[WorkflowConfig], AgentProviderProtocol]
EmitLine = Callable[[str], None]


async def run_real_worker(
    config: WorkflowConfig,
    dispatch: DispatchRequest,
    *,
    provider_factory: ProviderFactory | None = None,
    workspace_populator: WorkspacePopulator | None = None,
    emit: EmitLine | None = None,
) -> int:
    """Run a real worker attempt with injectable provider I/O for tests."""
    emit = emit or (lambda line: print(line, flush=True))
    provider_factory = provider_factory or _default_provider_factory
    host = config.remote.host or "remote-host"
    artifact_root = Path(dispatch.artifact_path)
    artifacts = ArtifactWriter(artifact_root, redact_keys=config.logging.redact_keys)
    issue = _issue_from_dispatch(dispatch)
    session: SessionRecord | None = None
    provider: AgentProviderProtocol | None = None
    terminal_state = Terminal.FAILED
    terminal_reason = "worker_failed"
    error: str | None = None
    workspace: Workspace | None = None

    try:
        _emit_worker_event(
            "worker_started",
            config=config,
            dispatch=dispatch,
            host=host,
            emit=emit,
            fields={"worker_id": f"{dispatch.issue_identifier}:{dispatch.attempt}"},
        )
        workspace = prepare_worker_workspace(
            config,
            dispatch,
            issue,
            populator=workspace_populator,
        )
        workspace_path = workspace.path
        manager = WorkspaceManager(config.workspace)
        if not workspace.reused:
            _ensure_hook_ok(manager.run_hook("after_create", workspace))
        _ensure_hook_ok(manager.run_hook("before_run", workspace))
        _emit_worker_event(
            "workspace_ready",
            config=config,
            dispatch=dispatch,
            host=host,
            emit=emit,
            fields={"workspace_path": str(workspace_path)},
        )

        artifacts.write_json(
            "request.json",
            {
                "issue_identifier": dispatch.issue_identifier,
                "workspace_path": str(workspace_path),
                "attempt": dispatch.attempt,
                "model": config.claude.model,
                "permission_mode": config.claude.permission_mode,
                "security_profile": config.security.profile,
                "execution": "remote",
            },
        )

        provider = provider_factory(config)
        session = await provider.start_session(issue, workspace_path, config.claude)
        session.attempt = dispatch.attempt
        session.artifact_dir = artifact_root
        artifacts.write_json("session.json", _session_snapshot(session))

        terminal_event: AgentEvent | None = None
        async for event in provider.send_input(
            session,
            f"first prompt for {dispatch.issue_identifier}",
        ):
            artifacts.append_event(event)
            session.last_event_at = event.timestamp
            if event.event == "session_started":
                _emit_worker_event(
                    "session_started",
                    config=config,
                    dispatch=dispatch,
                    host=host,
                    emit=emit,
                    fields={
                        "session_id": session.session_id,
                        "provider_session_id": (
                            event.payload.get("session_id")
                            or session.provider_session_id
                            or "unknown"
                        ),
                    },
                )
                _emit_worker_event(
                    "heartbeat",
                    config=config,
                    dispatch=dispatch,
                    host=host,
                    emit=emit,
                    fields={"status": "running"},
                )
            if event.event in TERMINAL_TURN_EVENTS:
                terminal_event = event
                break

        if terminal_event is None:
            raise RuntimeError("provider stream ended without terminal event")

        if terminal_event.event == "turn_completed":
            terminal_state = Terminal.COMPLETED
            terminal_reason = "turn_completed"
            _emit_worker_event(
                "turn_completed",
                config=config,
                dispatch=dispatch,
                host=host,
                emit=emit,
                fields={"terminal": terminal_event.event},
            )
            _emit_worker_event(
                "worker_completed",
                config=config,
                dispatch=dispatch,
                host=host,
                emit=emit,
                fields={
                    "exit_code": 0,
                    "artifact_path": str(artifact_root),
                    "artifacts_ready": True,
                },
            )
            if workspace is not None:
                _ensure_hook_ok(manager.run_hook("after_run", workspace))
            return 0

        terminal_reason = terminal_event.event
        error = terminal_event.payload.get("reason") or terminal_event.event
        if workspace is not None:
            _ensure_hook_ok(manager.run_hook("after_run", workspace))
        _emit_worker_failed(
            config=config,
            dispatch=dispatch,
            host=host,
            emit=emit,
            message=str(error),
            error_type="provider_terminal",
            retryable=terminal_event.event == "turn_cancelled",
        )
        return 1
    except Exception as exc:  # noqa: BLE001 - worker boundary must fail closed
        error = _redact_runtime_error(str(exc), config)
        _emit_worker_failed(
            config=config,
            dispatch=dispatch,
            host=host,
            emit=emit,
            message=error,
            error_type=type(exc).__name__,
            retryable=True,
        )
        return 1
    finally:
        if session is not None and provider is not None:
            try:
                await provider.close(session)
            except Exception:
                pass
            artifacts.write_json("session.json", _session_snapshot(session))
        artifacts.write_json(
            "terminal.json",
            {
                "terminal_state": terminal_state.value,
                "reason": terminal_reason,
                "error": error,
                "retryable": terminal_state != Terminal.COMPLETED,
                "execution": "remote",
            },
        )


def prepare_worker_workspace(
    config: WorkflowConfig,
    dispatch: DispatchRequest,
    issue: Issue,
    *,
    populator: WorkspacePopulator | None = None,
) -> Workspace:
    """Prepare the exact dispatch workspace path with workspace boundary semantics."""
    workspace_path = _validated_workspace_path(dispatch, config)
    workspace_path.parent.mkdir(parents=True, exist_ok=True)
    reused = workspace_path.exists()
    if not reused:
        workspace_path.mkdir(parents=True, exist_ok=False)
    elif not workspace_path.is_dir():
        raise ValueError(f"workspace path exists but is not a directory: {workspace_path}")
    if config.workspace.populate == "git" and populator is not None:
        populator.populate(workspace_path, issue, reused=reused)
    return Workspace(
        issue_identifier=issue.identifier,
        workspace_key=workspace_path.name,
        path=workspace_path,
        repo_path=workspace_path,
        created_at=datetime.now(timezone.utc),
        reused=reused,
    )


def _ensure_hook_ok(result) -> None:
    if result is None or result.succeeded:
        return
    if result.timed_out:
        raise RuntimeError(f"{result.name} hook timed out after {result.duration_ms}ms")
    raise RuntimeError(f"{result.name} hook failed with exit code {result.returncode}")


def _default_provider_factory(config: WorkflowConfig) -> AgentProviderProtocol:
    from symphony.provider import ClaudeCodeProvider

    return ClaudeCodeProvider()


def _validated_workspace_path(
    dispatch: DispatchRequest,
    config: WorkflowConfig,
) -> Path:
    if not config.remote.workspace_root:
        raise ValueError("remote.workspace_root is required")
    root = Path(config.remote.workspace_root)
    workspace = Path(dispatch.workspace_path)
    if not workspace.is_absolute():
        raise ValueError("dispatch.workspace_path must be absolute")
    try:
        workspace.relative_to(root)
    except ValueError as exc:
        raise ValueError("dispatch.workspace_path must be inside remote.workspace_root") from exc
    return workspace


def _issue_from_dispatch(dispatch: DispatchRequest) -> Issue:
    return Issue(
        id=dispatch.issue_identifier,
        number=dispatch.issue_number,
        identifier=dispatch.issue_identifier,
        owner=dispatch.owner,
        repo=dispatch.repo,
        title=f"Remote issue {dispatch.issue_identifier}",
        body="",
        state="open",
        url=f"https://github.com/{dispatch.owner}/{dispatch.repo}/issues/{dispatch.issue_number}",
    )


def _emit_worker_event(
    event: str,
    *,
    config: WorkflowConfig,
    dispatch: DispatchRequest,
    host: str,
    emit: EmitLine,
    fields: dict[str, Any],
) -> None:
    line = serialize_worker_event(
        WorkerEvent(
            event=event,
            timestamp=datetime.now(timezone.utc).isoformat(),
            issue_identifier=dispatch.issue_identifier,
            attempt=dispatch.attempt,
            host=host,
            fields=fields,
        ),
        redact_keys=config.logging.redact_keys,
    )
    emit(line)


def _emit_worker_failed(
    *,
    config: WorkflowConfig,
    dispatch: DispatchRequest,
    host: str,
    emit: EmitLine,
    message: str,
    error_type: str,
    retryable: bool,
) -> None:
    _emit_worker_event(
        "worker_failed",
        config=config,
        dispatch=dispatch,
        host=host,
        emit=emit,
        fields={
            "error_type": error_type,
            "message": _redact_runtime_error(message, config),
            "retryable": retryable,
        },
    )


def _redact_runtime_error(message: str, config: WorkflowConfig) -> str:
    return redact_text(
        message,
        redact_keys=config.logging.redact_keys,
        extra_secrets=(config.tracker.token,),
    )


def _session_snapshot(session: SessionRecord) -> dict[str, Any]:
    return {
        "session_id": session.session_id,
        "provider": session.provider,
        "provider_session_id": session.provider_session_id,
        "issue_identifier": session.issue_identifier,
        "issue_number": session.issue_number,
        "workspace_path": str(session.workspace_path),
        "artifact_dir": str(session.artifact_dir),
        "attempt": session.attempt,
        "turn_count": session.turn_count,
        "started_at": session.started_at,
        "last_event_at": session.last_event_at,
        "terminal_state": session.terminal_state.value if session.terminal_state else None,
    }

if __name__ == "__main__":
    sys.exit(main())
