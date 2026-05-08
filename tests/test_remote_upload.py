"""Tests for remote payload upload."""

from __future__ import annotations

import subprocess
from pathlib import Path

from symphony.config import build_config
from symphony.models import Issue
from symphony.remote.materialize import materialize_remote_dispatch_plan
from symphony.remote.plan import build_remote_dispatch_plan
from symphony.remote.upload import (
    PayloadUploadResult,
    SCPPayloadUploader,
)


def _minimal_config(tmp_path: Path) -> dict:
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


class FakePayloadUploadRunner:
    """Fake runner for testing."""

    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.last_args: list[str] | None = None

    def run(
        self,
        args: list[str],
        *,
        capture_output: bool = True,
        text: bool = True,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess:
        self.last_args = args
        return subprocess.CompletedProcess(
            args=args,
            returncode=self.returncode,
            stdout=self.stdout,
            stderr=self.stderr,
        )


def test_upload_success(tmp_path: Path):
    """Test successful upload of both payload files."""
    config = build_config(_minimal_config(tmp_path), workflow_path=tmp_path / "WORKFLOW.md")
    plan = build_remote_dispatch_plan(_issue(), attempt=1, config=config)
    materialize_remote_dispatch_plan(plan, config)

    runner = FakePayloadUploadRunner(returncode=0)
    uploader = SCPPayloadUploader(host="user@remote-host", runner=runner)

    result = uploader.upload(plan)

    assert isinstance(result, PayloadUploadResult)
    assert result.ok is True
    assert len(result.uploaded) == 2
    assert plan.remote_snapshot_path in result.uploaded
    assert plan.remote_dispatch_path in result.uploaded
    assert result.errors == ()


def test_upload_builds_correct_scp_commands(tmp_path: Path):
    """Test uploader builds correct scp commands."""
    config = build_config(_minimal_config(tmp_path), workflow_path=tmp_path / "WORKFLOW.md")
    plan = build_remote_dispatch_plan(_issue(), attempt=1, config=config)
    materialize_remote_dispatch_plan(plan, config)

    runner = FakePayloadUploadRunner(returncode=0)
    uploader = SCPPayloadUploader(host="user@remote-host", runner=runner)

    uploader.upload(plan)

    # Check that scp was called (last call is for dispatch file)
    assert runner.last_args is not None
    assert runner.last_args[0] == "scp"
    assert "-q" in runner.last_args
    assert f"user@remote-host:{plan.remote_dispatch_path}" in runner.last_args


def test_upload_missing_local_snapshot_file(tmp_path: Path):
    """Test upload fails when local snapshot file is missing."""
    config = build_config(_minimal_config(tmp_path), workflow_path=tmp_path / "WORKFLOW.md")
    plan = build_remote_dispatch_plan(_issue(), attempt=1, config=config)
    materialized = materialize_remote_dispatch_plan(plan, config)

    # Delete snapshot file
    materialized.snapshot_path.unlink()

    runner = FakePayloadUploadRunner(returncode=0)
    uploader = SCPPayloadUploader(host="user@remote-host", runner=runner)

    result = uploader.upload(plan)

    assert result.ok is False
    assert len(result.errors) >= 1
    assert any("snapshot" in err.lower() and "not found" in err.lower() for err in result.errors)


def test_upload_missing_local_dispatch_file(tmp_path: Path):
    """Test upload fails when local dispatch file is missing."""
    config = build_config(_minimal_config(tmp_path), workflow_path=tmp_path / "WORKFLOW.md")
    plan = build_remote_dispatch_plan(_issue(), attempt=1, config=config)
    materialized = materialize_remote_dispatch_plan(plan, config)

    # Delete dispatch file
    materialized.dispatch_path.unlink()

    runner = FakePayloadUploadRunner(returncode=0)
    uploader = SCPPayloadUploader(host="user@remote-host", runner=runner)

    result = uploader.upload(plan)

    assert result.ok is False
    assert len(result.errors) >= 1
    assert any("dispatch" in err.lower() and "not found" in err.lower() for err in result.errors)


def test_upload_scp_failure(tmp_path: Path):
    """Test upload handles scp command failure."""
    config = build_config(_minimal_config(tmp_path), workflow_path=tmp_path / "WORKFLOW.md")
    plan = build_remote_dispatch_plan(_issue(), attempt=1, config=config)
    materialize_remote_dispatch_plan(plan, config)

    runner = FakePayloadUploadRunner(returncode=1, stderr="Permission denied")
    uploader = SCPPayloadUploader(host="user@remote-host", runner=runner)

    result = uploader.upload(plan)

    assert result.ok is False
    assert len(result.errors) == 2  # Both uploads failed
    assert len(result.uploaded) == 0


def test_upload_partial_failure(tmp_path: Path):
    """Test upload handles partial failure (snapshot succeeds, dispatch fails)."""
    config = build_config(_minimal_config(tmp_path), workflow_path=tmp_path / "WORKFLOW.md")
    plan = build_remote_dispatch_plan(_issue(), attempt=1, config=config)
    materialize_remote_dispatch_plan(plan, config)

    class PartialFailureRunner:
        def __init__(self):
            self.call_count = 0

        def run(
            self,
            args: list[str],
            *,
            capture_output: bool = True,
            text: bool = True,
            timeout: float | None = None,
        ) -> subprocess.CompletedProcess:
            self.call_count += 1
            # First call (snapshot) succeeds, second call (dispatch) fails
            returncode = 0 if self.call_count == 1 else 1
            stderr = "" if self.call_count == 1 else "Connection refused"
            return subprocess.CompletedProcess(
                args=args, returncode=returncode, stdout="", stderr=stderr
            )

    runner = PartialFailureRunner()
    uploader = SCPPayloadUploader(host="user@remote-host", runner=runner)

    result = uploader.upload(plan)

    assert result.partial is True
    assert len(result.uploaded) == 1
    assert len(result.errors) == 1
    assert plan.remote_snapshot_path in result.uploaded


def test_upload_redacts_secrets_from_errors(tmp_path: Path):
    """Test upload redacts secrets from error messages."""
    config = build_config(_minimal_config(tmp_path), workflow_path=tmp_path / "WORKFLOW.md")
    plan = build_remote_dispatch_plan(_issue(), attempt=1, config=config)
    materialize_remote_dispatch_plan(plan, config)

    # Simulate error containing secret token
    runner = FakePayloadUploadRunner(
        returncode=1, stderr="Error: token=ghp_secret_12345 not authorized"
    )
    uploader = SCPPayloadUploader(
        host="user@remote-host",
        runner=runner,
        extra_secrets=("ghp_secret_12345",),
    )

    result = uploader.upload(plan)

    # Check that token is redacted from errors
    errors_text = " ".join(result.errors)
    assert "ghp_secret_12345" not in errors_text


def test_upload_command_does_not_include_secrets(tmp_path: Path):
    """Test scp command args do not include coordinator secrets."""
    config = build_config(_minimal_config(tmp_path), workflow_path=tmp_path / "WORKFLOW.md")
    plan = build_remote_dispatch_plan(_issue(), attempt=1, config=config)
    materialize_remote_dispatch_plan(plan, config)

    runner = FakePayloadUploadRunner(returncode=0)
    uploader = SCPPayloadUploader(
        host="user@remote-host",
        runner=runner,
        extra_secrets=(config.tracker.token,),
    )

    uploader.upload(plan)

    # Check command args don't contain token
    command_str = " ".join(runner.last_args or [])
    assert config.tracker.token not in command_str
    assert "ghp_coordinator_secret_token_12345" not in command_str


def test_upload_returns_remote_paths(tmp_path: Path):
    """Test upload result contains remote paths."""
    config = build_config(_minimal_config(tmp_path), workflow_path=tmp_path / "WORKFLOW.md")
    plan = build_remote_dispatch_plan(_issue(), attempt=1, config=config)
    materialize_remote_dispatch_plan(plan, config)

    runner = FakePayloadUploadRunner(returncode=0)
    uploader = SCPPayloadUploader(host="user@remote-host", runner=runner)

    result = uploader.upload(plan)

    # Uploaded paths should match plan
    assert plan.remote_snapshot_path in result.uploaded
    assert plan.remote_dispatch_path in result.uploaded
    assert all(path.startswith("/remote/") for path in result.uploaded)
