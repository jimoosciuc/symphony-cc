"""Tests for remote dispatch payload materialization."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from symphony.config import build_config
from symphony.models import Issue
from symphony.remote.dispatch import load_dispatch_request
from symphony.remote.materialize import (
    MaterializedRemoteDispatch,
    materialize_remote_dispatch_plan,
)
from symphony.remote.plan import build_remote_dispatch_plan
from symphony.remote.snapshot import REMOTE_TRACKER_TOKEN_PLACEHOLDER
from symphony.remote.transport import validate_remote_worker_config


def _minimal_config(tmp_path: Path) -> dict:
    return {
        "tracker": {
            "kind": "github",
            "owner": "test-owner",
            "repo": "test-repo",
            "token": "ghp_test_token_12345",
        },
        "agent": {"provider": "claude_code"},
        "workspace": {"root": str(tmp_path / "workspaces")},
        "claude": {
            "model": "claude-fake",
            "permission_mode": "acceptEdits",
            "session_store": str(tmp_path / "sessions"),
            "transcript_store": str(tmp_path / "transcripts"),
            "artifact_store": str(tmp_path / "artifacts"),
        },
        "github": {},
        "remote": {
            "enabled": True,
            "host": "user@remote-host",
            "workspace_root": "/remote/workspaces",
            "artifact_root": "/remote/artifacts",
            "session_store": "/remote/sessions",
        },
    }


def _config(tmp_path: Path):
    return build_config(_minimal_config(tmp_path), workflow_path=tmp_path / "WORKFLOW.md")


def _issue() -> Issue:
    return Issue(
        id="test-owner/test-repo#42",
        number=42,
        identifier="test-owner/test-repo#42",
        owner="test-owner",
        repo="test-repo",
        title="Test issue",
        body="Test body",
        state="open",
        url="https://github.com/test-owner/test-repo/issues/42",
    )


def test_materialize_remote_dispatch_plan_writes_payload_files(tmp_path: Path):
    """Test materializer writes snapshot and dispatch files."""
    config = _config(tmp_path)
    plan = build_remote_dispatch_plan(_issue(), attempt=1, config=config)

    result = materialize_remote_dispatch_plan(plan, config)

    assert isinstance(result, MaterializedRemoteDispatch)
    assert result.snapshot_path == plan.local_snapshot_path
    assert result.dispatch_path == plan.local_dispatch_path
    assert result.snapshot_bytes == len(result.snapshot_path.read_bytes())
    assert result.dispatch_bytes == len(result.dispatch_path.read_bytes())


def test_materialized_dispatch_request_round_trips(tmp_path: Path):
    """Test dispatch payload round-trips through the request loader."""
    config = _config(tmp_path)
    plan = build_remote_dispatch_plan(_issue(), attempt=2, config=config)

    materialize_remote_dispatch_plan(plan, config)

    loaded = load_dispatch_request(plan.local_dispatch_path)
    assert loaded == plan.dispatch_request
    assert loaded.workspace_path == "/remote/workspaces/test-owner/test-repo/42"
    assert loaded.artifact_path == "/remote/artifacts/test-owner/test-repo/42/attempt-2"


def test_materialized_snapshot_validates_as_remote_worker_config(tmp_path: Path):
    """Test snapshot can be loaded as worker config and validated."""
    config = _config(tmp_path)
    plan = build_remote_dispatch_plan(_issue(), attempt=1, config=config)

    materialize_remote_dispatch_plan(plan, config)

    snapshot_config = build_config(
        json.loads(plan.local_snapshot_path.read_text(encoding="utf-8")),
        workflow_path=tmp_path / "SNAPSHOT.md",
    )
    validate_remote_worker_config(snapshot_config)
    assert str(snapshot_config.workspace.root) == plan.remote_workspace_path
    assert snapshot_config.remote.workspace_root == config.remote.workspace_root
    assert snapshot_config.remote.artifact_root == config.remote.artifact_root


def test_materialized_snapshot_omits_coordinator_tracker_token(tmp_path: Path):
    """Test snapshot does not expose coordinator tracker credentials."""
    config = _config(tmp_path)
    plan = build_remote_dispatch_plan(_issue(), attempt=1, config=config)

    materialize_remote_dispatch_plan(plan, config)

    snapshot_text = plan.local_snapshot_path.read_text(encoding="utf-8")
    snapshot = json.loads(snapshot_text)
    assert config.tracker.token not in snapshot_text
    assert "ghp_" not in snapshot_text
    assert snapshot["tracker"]["token"] == REMOTE_TRACKER_TOKEN_PLACEHOLDER


def test_materialize_creates_missing_parent_directories(tmp_path: Path):
    """Test materializer creates planned parent directories."""
    config = _config(tmp_path)
    plan = build_remote_dispatch_plan(_issue(), attempt=1, config=config)

    assert not plan.local_snapshot_path.parent.exists()

    materialize_remote_dispatch_plan(plan, config)

    assert plan.local_snapshot_path.exists()
    assert plan.local_dispatch_path.exists()


def test_materialize_overwrites_existing_files_deterministically(tmp_path: Path):
    """Test existing payload files are overwritten with deterministic content."""
    config = _config(tmp_path)
    plan = build_remote_dispatch_plan(_issue(), attempt=1, config=config)

    first = materialize_remote_dispatch_plan(plan, config)
    plan.local_snapshot_path.write_text("stale snapshot", encoding="utf-8")
    plan.local_dispatch_path.write_text("stale dispatch", encoding="utf-8")
    second = materialize_remote_dispatch_plan(plan, config)

    assert first.snapshot_bytes == second.snapshot_bytes
    assert first.dispatch_bytes == second.dispatch_bytes
    assert json.loads(plan.local_snapshot_path.read_text(encoding="utf-8"))
    assert load_dispatch_request(plan.local_dispatch_path) == plan.dispatch_request


def test_materialize_write_failure_propagates(tmp_path: Path):
    """Test filesystem write failures surface to the caller."""
    config = _config(tmp_path)
    plan = build_remote_dispatch_plan(_issue(), attempt=1, config=config)
    plan.local_snapshot_path.parent.mkdir(parents=True)
    plan.local_snapshot_path.mkdir()

    broken_plan = replace(plan, local_dispatch_path=tmp_path / "dispatch.json")

    with pytest.raises(IsADirectoryError):
        materialize_remote_dispatch_plan(broken_plan, config)

    assert not broken_plan.local_dispatch_path.exists()
