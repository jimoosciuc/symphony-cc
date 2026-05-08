"""Coordinator-side planning for remote worker dispatch.

This module is intentionally pure: it computes deterministic paths and the
remote worker dispatch request, but it does not write files, spawn SSH, call
Claude, or talk to the tracker.
"""

from __future__ import annotations

import posixpath
from dataclasses import dataclass
from pathlib import Path

from symphony.config import WorkflowConfig
from symphony.github.pr import expected_branch_name
from symphony.models import Issue
from symphony.remote.dispatch import DispatchRequest, serialize_dispatch_request


@dataclass(frozen=True, slots=True)
class RemoteDispatchPlan:
    """Pure data needed by later remote-dispatch execution steps."""

    dispatch_request: DispatchRequest
    local_snapshot_path: Path
    local_dispatch_path: Path
    remote_workspace_path: str
    remote_artifact_path: str

    def serialize_dispatch_request(self) -> str:
        """Serialize the remote-bound dispatch request."""
        return serialize_dispatch_request(self.dispatch_request)


def remote_issue_path(root: str, issue: Issue, *extra_segments: str) -> str:
    """Return a deterministic remote path rooted under ``root`` for ``issue``."""
    return _join_remote_path(
        root,
        issue.owner,
        issue.repo,
        str(issue.number),
        *extra_segments,
    )


def local_issue_path(root: Path, issue: Issue, *extra_segments: str) -> Path:
    """Return a deterministic coordinator-local path for remote plan files."""
    return root.joinpath(
        ".remote",
        _safe_segment(issue.owner),
        _safe_segment(issue.repo),
        str(issue.number),
        *(_safe_segment(segment) for segment in extra_segments),
    )


def build_remote_dispatch_plan(
    issue: Issue,
    attempt: int,
    config: WorkflowConfig,
) -> RemoteDispatchPlan:
    """Build a pure remote dispatch plan for an issue attempt."""
    if attempt < 1:
        raise ValueError("attempt must be >= 1")

    if not config.remote.enabled:
        raise ValueError("remote.enabled must be true for remote dispatch")

    if not config.remote.workspace_root:
        raise ValueError("remote.workspace_root is required for remote dispatch")

    if not config.remote.artifact_root:
        raise ValueError("remote.artifact_root is required for remote dispatch")

    if not config.remote.host:
        raise ValueError("remote.host is required for remote dispatch")

    attempt_segment = f"attempt-{attempt}"
    remote_workspace_path = remote_issue_path(config.remote.workspace_root, issue)
    remote_artifact_path = remote_issue_path(
        config.remote.artifact_root,
        issue,
        attempt_segment,
    )
    local_snapshot_path = local_issue_path(
        config.workspace.root,
        issue,
        attempt_segment,
        "snapshot.json",
    )
    local_dispatch_path = local_issue_path(
        config.workspace.root,
        issue,
        attempt_segment,
        "dispatch.json",
    )

    dispatch_request = DispatchRequest(
        owner=issue.owner,
        repo=issue.repo,
        issue_number=issue.number,
        attempt=attempt,
        workspace_path=remote_workspace_path,
        artifact_path=remote_artifact_path,
        branch=expected_branch_name(config.github, issue),
        base_branch=config.github.base_branch,
    )

    return RemoteDispatchPlan(
        dispatch_request=dispatch_request,
        local_snapshot_path=local_snapshot_path,
        local_dispatch_path=local_dispatch_path,
        remote_workspace_path=remote_workspace_path,
        remote_artifact_path=remote_artifact_path,
    )


def _join_remote_path(root: str, *segments: str) -> str:
    if not root.strip():
        raise ValueError("remote root cannot be empty")
    if not root.startswith("/"):
        raise ValueError("remote root must be an absolute POSIX path")

    clean_root = posixpath.normpath(root)
    if clean_root == ".":
        raise ValueError("remote root cannot be empty")

    return posixpath.join(clean_root, *(_safe_segment(segment) for segment in segments))


def _safe_segment(segment: str) -> str:
    if not segment or segment in {".", ".."}:
        raise ValueError("path segment cannot be empty, '.', or '..'")
    if "/" in segment or "\\" in segment:
        raise ValueError(f"path segment cannot contain separators: {segment!r}")
    return segment
