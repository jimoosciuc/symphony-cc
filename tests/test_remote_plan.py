"""Tests for remote dispatch plan builder."""

from dataclasses import asdict, replace
from pathlib import Path

import pytest

from symphony.config import ConfigError, build_config
from symphony.models import Issue
from symphony.remote.plan import RemoteDispatchPlan, build_remote_dispatch_plan


def _minimal_config(tmp_path: Path) -> dict:
    """Return minimal valid workflow config with remote enabled."""
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


def test_build_remote_dispatch_plan_basic(tmp_path: Path):
    """Test building basic remote dispatch plan."""
    config = _config(tmp_path)
    issue = _make_issue("test-owner", "test-repo", 42)

    plan = build_remote_dispatch_plan(issue, attempt=1, config=config)

    assert isinstance(plan, RemoteDispatchPlan)
    assert plan.dispatch_request.owner == "test-owner"
    assert plan.dispatch_request.repo == "test-repo"
    assert plan.dispatch_request.issue_number == 42
    assert plan.dispatch_request.attempt == 1
    assert plan.dispatch_request.workspace_path == "/remote/workspaces/test-owner/test-repo/42"
    assert plan.dispatch_request.artifact_path == (
        "/remote/artifacts/test-owner/test-repo/42/attempt-1"
    )
    assert plan.remote_snapshot_path == (
        "/remote/workspaces/test-owner/test-repo/42/.symphony/attempt-1/snapshot.json"
    )
    assert plan.remote_dispatch_path == (
        "/remote/workspaces/test-owner/test-repo/42/.symphony/attempt-1/dispatch.json"
    )
    assert plan.dispatch_request.branch == "symphony/test-owner-test-repo-42"
    assert plan.dispatch_request.base_branch == "main"


def test_build_remote_dispatch_plan_carries_rendered_prompt(tmp_path: Path):
    config = _config(tmp_path)
    issue = _make_issue("test-owner", "test-repo", 42)

    plan = build_remote_dispatch_plan(
        issue,
        attempt=1,
        config=config,
        prompt="Rendered workflow prompt",
    )

    assert plan.dispatch_request.prompt_ref == "Rendered workflow prompt"
    assert "Rendered workflow prompt" in plan.serialize_dispatch_request()


def test_build_remote_dispatch_plan_paths_deterministic(tmp_path: Path):
    """Test remote dispatch plan paths are deterministic."""
    config = _config(tmp_path)
    issue = _make_issue("test-owner", "test-repo", 42)

    plan1 = build_remote_dispatch_plan(issue, attempt=1, config=config)
    plan2 = build_remote_dispatch_plan(issue, attempt=1, config=config)

    assert plan1.remote_workspace_path == plan2.remote_workspace_path
    assert plan1.remote_artifact_path == plan2.remote_artifact_path
    assert plan1.local_snapshot_path == plan2.local_snapshot_path
    assert plan1.local_dispatch_path == plan2.local_dispatch_path
    assert plan1.remote_snapshot_path == plan2.remote_snapshot_path
    assert plan1.remote_dispatch_path == plan2.remote_dispatch_path


def test_build_remote_dispatch_plan_different_attempts(tmp_path: Path):
    """Test attempt-specific paths differ by attempt."""
    config = _config(tmp_path)
    issue = _make_issue("test-owner", "test-repo", 42)

    plan1 = build_remote_dispatch_plan(issue, attempt=1, config=config)
    plan2 = build_remote_dispatch_plan(issue, attempt=2, config=config)

    assert plan1.remote_workspace_path == plan2.remote_workspace_path
    assert plan1.remote_artifact_path != plan2.remote_artifact_path
    assert plan1.remote_artifact_path.endswith("/attempt-1")
    assert plan2.remote_artifact_path.endswith("/attempt-2")
    assert plan1.local_snapshot_path != plan2.local_snapshot_path
    assert plan1.local_dispatch_path != plan2.local_dispatch_path
    assert plan1.remote_snapshot_path.endswith("/attempt-1/snapshot.json")
    assert plan2.remote_snapshot_path.endswith("/attempt-2/snapshot.json")
    assert plan1.remote_dispatch_path.endswith("/attempt-1/dispatch.json")
    assert plan2.remote_dispatch_path.endswith("/attempt-2/dispatch.json")


def test_build_remote_dispatch_plan_local_paths_under_workspace_root(tmp_path: Path):
    """Test local snapshot/dispatch paths are under workspace root."""
    config = _config(tmp_path)
    issue = _make_issue("test-owner", "test-repo", 42)

    plan = build_remote_dispatch_plan(issue, attempt=1, config=config)

    assert plan.local_snapshot_path.is_relative_to(config.workspace.root)
    assert plan.local_dispatch_path.is_relative_to(config.workspace.root)
    assert plan.local_snapshot_path.name == "snapshot.json"
    assert plan.local_dispatch_path.name == "dispatch.json"
    assert ".remote" in plan.local_snapshot_path.parts
    assert "attempt-1" in plan.local_snapshot_path.parts


def test_build_remote_dispatch_plan_remote_paths_under_remote_roots(tmp_path: Path):
    """Test remote paths are under configured remote roots."""
    config = _config(tmp_path)
    issue = _make_issue("test-owner", "test-repo", 42)

    plan = build_remote_dispatch_plan(issue, attempt=1, config=config)

    assert plan.remote_workspace_path.startswith(config.remote.workspace_root)
    assert plan.remote_artifact_path.startswith(config.remote.artifact_root)
    assert plan.remote_snapshot_path.startswith(plan.remote_workspace_path)
    assert plan.remote_dispatch_path.startswith(plan.remote_workspace_path)


def test_build_remote_dispatch_plan_no_tracker_token_in_dispatch_request(tmp_path: Path):
    """Test dispatch request does not contain tracker token."""
    config = _config(tmp_path)
    issue = _make_issue("test-owner", "test-repo", 42)

    plan = build_remote_dispatch_plan(issue, attempt=1, config=config)

    serialized = plan.serialize_dispatch_request()
    assert config.tracker.token not in serialized
    assert "ghp_" not in serialized


def test_build_remote_dispatch_plan_no_tracker_token_in_plan_fields(tmp_path: Path):
    """Test plan data itself does not carry the coordinator tracker token."""
    config = _config(tmp_path)
    issue = _make_issue("test-owner", "test-repo", 42)

    plan = build_remote_dispatch_plan(issue, attempt=1, config=config)

    serialized_plan = repr(asdict(plan))
    assert config.tracker.token not in serialized_plan
    assert "ghp_" not in serialized_plan


def test_build_remote_dispatch_plan_rejects_disabled_remote(tmp_path: Path):
    """Test plan builder rejects disabled remote."""
    raw = _minimal_config(tmp_path)
    raw["remote"]["enabled"] = False
    config = build_config(raw, workflow_path=tmp_path / "WORKFLOW.md")
    issue = _make_issue("test-owner", "test-repo", 42)

    with pytest.raises(ValueError, match="remote.enabled must be true"):
        build_remote_dispatch_plan(issue, attempt=1, config=config)


def test_build_remote_dispatch_plan_rejects_missing_workspace_root(tmp_path: Path):
    """Test plan builder rejects missing workspace_root."""
    config = _config(tmp_path)
    config = replace(config, remote=replace(config.remote, workspace_root=None))
    issue = _make_issue("test-owner", "test-repo", 42)

    with pytest.raises(ValueError, match="remote.workspace_root is required"):
        build_remote_dispatch_plan(issue, attempt=1, config=config)


def test_build_remote_dispatch_plan_rejects_missing_artifact_root(tmp_path: Path):
    """Test plan builder rejects missing artifact_root."""
    config = _config(tmp_path)
    config = replace(config, remote=replace(config.remote, artifact_root=None))
    issue = _make_issue("test-owner", "test-repo", 42)

    with pytest.raises(ValueError, match="remote.artifact_root is required"):
        build_remote_dispatch_plan(issue, attempt=1, config=config)


def test_build_remote_dispatch_plan_rejects_missing_host(tmp_path: Path):
    """Test plan builder rejects missing host."""
    config = _config(tmp_path)
    config = replace(config, remote=replace(config.remote, host=None))
    issue = _make_issue("test-owner", "test-repo", 42)

    with pytest.raises(ValueError, match="remote.host is required"):
        build_remote_dispatch_plan(issue, attempt=1, config=config)


def test_build_remote_dispatch_plan_rejects_non_positive_attempt(tmp_path: Path):
    """Test plan builder rejects invalid attempt numbers explicitly."""
    config = _config(tmp_path)
    issue = _make_issue("test-owner", "test-repo", 42)

    with pytest.raises(ValueError, match="attempt must be >= 1"):
        build_remote_dispatch_plan(issue, attempt=0, config=config)


def test_build_remote_dispatch_plan_rejects_relative_remote_root(tmp_path: Path):
    """Test remote roots must be absolute POSIX paths."""
    config = _config(tmp_path)
    config = replace(config, remote=replace(config.remote, workspace_root="relative/workspaces"))
    issue = _make_issue("test-owner", "test-repo", 42)

    with pytest.raises(ValueError, match="remote root must be an absolute POSIX path"):
        build_remote_dispatch_plan(issue, attempt=1, config=config)


def test_build_config_rejects_enabled_remote_missing_required_fields(tmp_path: Path):
    """Test config validation rejects incomplete enabled remote config."""
    raw = _minimal_config(tmp_path)
    del raw["remote"]["workspace_root"]

    with pytest.raises(ConfigError, match="remote.workspace_root: required"):
        build_config(raw, workflow_path=tmp_path / "WORKFLOW.md")


def test_build_remote_dispatch_plan_rejects_unsafe_issue_path_segment(tmp_path: Path):
    """Test issue identity cannot escape remote/local path roots."""
    config = _config(tmp_path)
    issue = _make_issue("test-owner", "../repo", 42)

    with pytest.raises(ValueError, match="path segment cannot contain separators"):
        build_remote_dispatch_plan(issue, attempt=1, config=config)
