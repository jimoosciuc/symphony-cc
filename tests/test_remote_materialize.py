"""Tests for remote dispatch payload materialization."""

import json
from pathlib import Path

from symphony.config import build_config
from symphony.models import Issue
from symphony.remote.dispatch import load_dispatch_request
from symphony.remote.materialize import (
    REMOTE_TRACKER_TOKEN_PLACEHOLDER,
    MaterializeResult,
    materialize_remote_dispatch_plan,
)
from symphony.remote.plan import build_remote_dispatch_plan


def _minimal_config(tmp_path: Path) -> dict:
    """Return minimal valid workflow config with remote enabled."""
    return {
        "tracker": {
            "kind": "github",
            "owner": "test-owner",
            "repo": "test-repo",
            "token": "ghp_coordinator_secret_token_12345",
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


def _make_issue(owner: str, repo: str, number: int) -> Issue:
    """Create test issue with required fields."""
    return Issue(
        id=f"{owner}/{repo}#{number}",
        number=number,
        identifier=f"{owner}/{repo}#{number}",
        owner=owner,
        repo=repo,
        title="Test issue",
        body="Test body",
        state="open",
        url=f"https://github.com/{owner}/{repo}/issues/{number}",
    )


def test_materialize_remote_dispatch_plan_writes_both_files(tmp_path: Path):
    """Test materialize writes both snapshot and dispatch files."""
    raw = _minimal_config(tmp_path)
    config = build_config(raw, workflow_path=tmp_path / "WORKFLOW.md")
    issue = _make_issue("test-owner", "test-repo", 42)
    plan = build_remote_dispatch_plan(issue, attempt=1, config=config)

    result = materialize_remote_dispatch_plan(plan, config)

    assert isinstance(result, MaterializeResult)
    assert result.snapshot_path == plan.local_snapshot_path
    assert result.dispatch_path == plan.local_dispatch_path
    assert result.snapshot_path.exists()
    assert result.dispatch_path.exists()
    assert result.snapshot_bytes > 0
    assert result.dispatch_bytes > 0


def test_materialize_creates_parent_directories(tmp_path: Path):
    """Test materialize creates parent directories if missing."""
    raw = _minimal_config(tmp_path)
    config = build_config(raw, workflow_path=tmp_path / "WORKFLOW.md")
    issue = _make_issue("test-owner", "test-repo", 42)
    plan = build_remote_dispatch_plan(issue, attempt=1, config=config)

    # Ensure parent directories don't exist
    assert not plan.local_snapshot_path.parent.exists()
    assert not plan.local_dispatch_path.parent.exists()

    result = materialize_remote_dispatch_plan(plan, config)

    assert plan.local_snapshot_path.parent.exists()
    assert plan.local_dispatch_path.parent.exists()
    assert result.snapshot_path.exists()
    assert result.dispatch_path.exists()


def test_materialize_overwrites_existing_files(tmp_path: Path):
    """Test materialize overwrites existing files deterministically."""
    raw = _minimal_config(tmp_path)
    config = build_config(raw, workflow_path=tmp_path / "WORKFLOW.md")
    issue = _make_issue("test-owner", "test-repo", 42)
    plan = build_remote_dispatch_plan(issue, attempt=1, config=config)

    # Write files first time
    result1 = materialize_remote_dispatch_plan(plan, config)

    # Write files second time (should overwrite)
    result2 = materialize_remote_dispatch_plan(plan, config)

    assert result1.snapshot_path == result2.snapshot_path
    assert result1.dispatch_path == result2.dispatch_path
    assert result1.snapshot_bytes == result2.snapshot_bytes
    assert result1.dispatch_bytes == result2.dispatch_bytes


def test_materialize_dispatch_file_roundtrips(tmp_path: Path):
    """Test dispatch file can be loaded back via load_dispatch_request."""
    raw = _minimal_config(tmp_path)
    config = build_config(raw, workflow_path=tmp_path / "WORKFLOW.md")
    issue = _make_issue("test-owner", "test-repo", 42)
    plan = build_remote_dispatch_plan(issue, attempt=1, config=config)

    materialize_remote_dispatch_plan(plan, config)

    # Load dispatch request back
    loaded = load_dispatch_request(plan.local_dispatch_path)

    assert loaded.owner == issue.owner
    assert loaded.repo == issue.repo
    assert loaded.issue_number == issue.number
    assert loaded.attempt == 1
    assert loaded.workspace_path == plan.remote_workspace_path
    assert loaded.artifact_path == plan.remote_artifact_path


def test_materialize_snapshot_file_roundtrips(tmp_path: Path):
    """Test snapshot file can be loaded back via build_config."""
    raw = _minimal_config(tmp_path)
    config = build_config(raw, workflow_path=tmp_path / "WORKFLOW.md")
    issue = _make_issue("test-owner", "test-repo", 42)
    plan = build_remote_dispatch_plan(issue, attempt=1, config=config)

    materialize_remote_dispatch_plan(plan, config)

    # Load snapshot back
    snapshot_raw = json.loads(plan.local_snapshot_path.read_text(encoding="utf-8"))
    loaded_config = build_config(snapshot_raw, workflow_path=tmp_path / "WORKFLOW.md")

    assert loaded_config.tracker.kind == config.tracker.kind
    assert loaded_config.tracker.owner == config.tracker.owner
    assert loaded_config.tracker.repo == config.tracker.repo
    assert loaded_config.agent.provider == config.agent.provider
    assert loaded_config.remote.enabled == config.remote.enabled
    assert loaded_config.remote.host == config.remote.host


def test_materialize_snapshot_excludes_coordinator_tracker_token(tmp_path: Path):
    """Test snapshot does not contain coordinator tracker token."""
    raw = _minimal_config(tmp_path)
    config = build_config(raw, workflow_path=tmp_path / "WORKFLOW.md")
    issue = _make_issue("test-owner", "test-repo", 42)
    plan = build_remote_dispatch_plan(issue, attempt=1, config=config)

    materialize_remote_dispatch_plan(plan, config)

    snapshot_text = plan.local_snapshot_path.read_text(encoding="utf-8")
    snapshot_raw = json.loads(snapshot_text)

    # Coordinator token should not appear
    assert config.tracker.token not in snapshot_text
    assert "ghp_coordinator_secret_token_12345" not in snapshot_text

    # Placeholder should be present
    assert snapshot_raw["tracker"]["token"] == REMOTE_TRACKER_TOKEN_PLACEHOLDER


def test_materialize_snapshot_contains_required_sections(tmp_path: Path):
    """Test snapshot contains all worker-required config sections."""
    raw = _minimal_config(tmp_path)
    config = build_config(raw, workflow_path=tmp_path / "WORKFLOW.md")
    issue = _make_issue("test-owner", "test-repo", 42)
    plan = build_remote_dispatch_plan(issue, attempt=1, config=config)

    materialize_remote_dispatch_plan(plan, config)

    snapshot_raw = json.loads(plan.local_snapshot_path.read_text(encoding="utf-8"))

    # Check required sections exist
    assert "tracker" in snapshot_raw
    assert "agent" in snapshot_raw
    assert "workspace" in snapshot_raw
    assert "claude" in snapshot_raw
    assert "github" in snapshot_raw
    assert "remote" in snapshot_raw

    # Check tracker section
    assert snapshot_raw["tracker"]["kind"] == "github"
    assert snapshot_raw["tracker"]["owner"] == "test-owner"
    assert snapshot_raw["tracker"]["repo"] == "test-repo"

    # Check remote section
    assert snapshot_raw["remote"]["enabled"] is True
    assert snapshot_raw["remote"]["host"] == "user@remote-host"
    assert snapshot_raw["remote"]["workspace_root"] == "/remote/workspaces"
    assert snapshot_raw["remote"]["artifact_root"] == "/remote/artifacts"


def test_materialize_dispatch_excludes_coordinator_secrets(tmp_path: Path):
    """Test dispatch request does not contain coordinator secrets."""
    raw = _minimal_config(tmp_path)
    config = build_config(raw, workflow_path=tmp_path / "WORKFLOW.md")
    issue = _make_issue("test-owner", "test-repo", 42)
    plan = build_remote_dispatch_plan(issue, attempt=1, config=config)

    materialize_remote_dispatch_plan(plan, config)

    dispatch_text = plan.local_dispatch_path.read_text(encoding="utf-8")

    # Coordinator token should not appear in dispatch
    assert config.tracker.token not in dispatch_text
    assert "ghp_" not in dispatch_text


def test_materialize_different_attempts_write_different_paths(tmp_path: Path):
    """Test different attempts write to different paths."""
    raw = _minimal_config(tmp_path)
    config = build_config(raw, workflow_path=tmp_path / "WORKFLOW.md")
    issue = _make_issue("test-owner", "test-repo", 42)

    plan1 = build_remote_dispatch_plan(issue, attempt=1, config=config)
    plan2 = build_remote_dispatch_plan(issue, attempt=2, config=config)

    result1 = materialize_remote_dispatch_plan(plan1, config)
    result2 = materialize_remote_dispatch_plan(plan2, config)

    # Different paths
    assert result1.snapshot_path != result2.snapshot_path
    assert result1.dispatch_path != result2.dispatch_path

    # Both exist
    assert result1.snapshot_path.exists()
    assert result1.dispatch_path.exists()
    assert result2.snapshot_path.exists()
    assert result2.dispatch_path.exists()

    # Different dispatch content (different attempt numbers)
    dispatch1 = json.loads(result1.dispatch_path.read_text(encoding="utf-8"))
    dispatch2 = json.loads(result2.dispatch_path.read_text(encoding="utf-8"))
    assert dispatch1["attempt"] == 1
    assert dispatch2["attempt"] == 2


def test_materialize_snapshot_no_token_shaped_values(tmp_path: Path):
    """Test snapshot does not contain token-shaped secret values."""
    raw = _minimal_config(tmp_path)
    # Add various token-shaped values to config
    raw["tracker"]["token"] = "ghp_test_secret_12345"
    config = build_config(raw, workflow_path=tmp_path / "WORKFLOW.md")
    issue = _make_issue("test-owner", "test-repo", 42)
    plan = build_remote_dispatch_plan(issue, attempt=1, config=config)

    materialize_remote_dispatch_plan(plan, config)

    snapshot_text = plan.local_snapshot_path.read_text(encoding="utf-8")

    # No token-shaped values should appear
    assert "ghp_test_secret_12345" not in snapshot_text
    assert "ghp_" not in snapshot_text or snapshot_text.count("ghp_") == 0
