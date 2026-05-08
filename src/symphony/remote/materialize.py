"""Materialize coordinator-side remote dispatch payload files."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from symphony.config import WorkflowConfig
from symphony.remote.dispatch import serialize_dispatch_request
from symphony.remote.plan import RemoteDispatchPlan
from symphony.remote.snapshot import serialize_config_snapshot


@dataclass(frozen=True, slots=True)
class MaterializedRemoteDispatch:
    """Files written for a remote dispatch plan."""

    snapshot_path: Path
    dispatch_path: Path
    snapshot_bytes: int
    dispatch_bytes: int


def materialize_remote_dispatch_plan(
    plan: RemoteDispatchPlan,
    config: WorkflowConfig,
) -> MaterializedRemoteDispatch:
    """Write the remote-safe config snapshot and dispatch request locally.

    Existing files are overwritten deterministically. Parent directories are
    created as needed. Any filesystem failure is allowed to propagate so the
    coordinator can mark the attempt failed with the original cause.
    """

    snapshot_json = serialize_config_snapshot(
        config,
        workspace_root=plan.remote_workspace_path,
    )
    dispatch_json = serialize_dispatch_request(plan.dispatch_request)

    plan.local_snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    plan.local_dispatch_path.parent.mkdir(parents=True, exist_ok=True)

    plan.local_snapshot_path.write_text(snapshot_json, encoding="utf-8")
    plan.local_dispatch_path.write_text(dispatch_json, encoding="utf-8")

    return MaterializedRemoteDispatch(
        snapshot_path=plan.local_snapshot_path,
        dispatch_path=plan.local_dispatch_path,
        snapshot_bytes=len(snapshot_json.encode("utf-8")),
        dispatch_bytes=len(dispatch_json.encode("utf-8")),
    )
