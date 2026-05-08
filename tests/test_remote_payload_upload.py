"""Tests for remote dispatch payload upload staging."""

from __future__ import annotations

import subprocess
from pathlib import Path

from symphony.artifacts import REDACTED
from symphony.config import build_config
from symphony.models import Issue
from symphony.remote.materialize import materialize_remote_dispatch_plan
from symphony.remote.plan import build_remote_dispatch_plan
from symphony.remote.upload import PayloadUploadResult, SCPPayloadUploader


class FakeUploadRunner:
    def __init__(
        self,
        *,
        returncodes: list[int] | None = None,
        stderrs: list[str] | None = None,
        raise_errors: list[Exception | None] | None = None,
    ) -> None:
        self.returncodes = returncodes or [0, 0]
        self.stderrs = stderrs or ["", ""]
        self.raise_errors = raise_errors or [None, None]
        self.calls: list[list[str]] = []

    def run(
        self,
        args: list[str],
        *,
        capture_output: bool = True,
        text: bool = True,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess:
        self.calls.append(args)
        index = len(self.calls) - 1
        error = self.raise_errors[index] if index < len(self.raise_errors) else None
        if error is not None:
            raise error
        returncode = self.returncodes[index] if index < len(self.returncodes) else 0
        stderr = self.stderrs[index] if index < len(self.stderrs) else ""
        return subprocess.CompletedProcess(args=args, returncode=returncode, stderr=stderr)


def _minimal_config(tmp_path: Path) -> dict:
    return {
        "tracker": {
            "kind": "github",
            "owner": "test-owner",
            "repo": "test-repo",
            "token": "ghp_testtokenabcdefghijklmnopqrstuvwxyz",
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


def _materialized_plan(tmp_path: Path):
    config = _config(tmp_path)
    plan = build_remote_dispatch_plan(_issue(), attempt=1, config=config)
    materialize_remote_dispatch_plan(plan, config)
    return config, plan


def test_scp_payload_uploader_uploads_snapshot_and_dispatch(tmp_path: Path):
    """Test uploader stages both materialized payload files."""
    config, plan = _materialized_plan(tmp_path)
    runner = FakeUploadRunner()
    uploader = SCPPayloadUploader(
        host=config.remote.host or "",
        runner=runner,
        extra_secrets=(config.tracker.token,),
    )

    result = uploader.upload(plan)

    assert isinstance(result, PayloadUploadResult)
    assert result.ok
    assert not result.partial
    assert result.uploaded == (plan.remote_snapshot_path, plan.remote_dispatch_path)
    assert len(runner.calls) == 2
    assert runner.calls[0] == [
        "scp",
        "-q",
        str(plan.local_snapshot_path),
        f"{config.remote.host}:{plan.remote_snapshot_path}",
    ]
    assert runner.calls[1] == [
        "scp",
        "-q",
        str(plan.local_dispatch_path),
        f"{config.remote.host}:{plan.remote_dispatch_path}",
    ]


def test_scp_payload_uploader_command_omits_tracker_token(tmp_path: Path):
    """Test upload commands do not contain coordinator tracker token."""
    config, plan = _materialized_plan(tmp_path)
    runner = FakeUploadRunner()
    uploader = SCPPayloadUploader(
        host=config.remote.host or "",
        runner=runner,
        extra_secrets=(config.tracker.token,),
    )

    uploader.upload(plan)

    command_text = " ".join(" ".join(call) for call in runner.calls)
    assert config.tracker.token not in command_text
    assert "ghp_" not in command_text


def test_scp_payload_uploader_missing_local_file_fails_clearly(tmp_path: Path):
    """Test missing local payload files fail without running scp for that file."""
    config, plan = _materialized_plan(tmp_path)
    plan.local_snapshot_path.unlink()
    runner = FakeUploadRunner()
    uploader = SCPPayloadUploader(host=config.remote.host or "", runner=runner)

    result = uploader.upload(plan)

    assert not result.ok
    assert result.partial
    assert result.uploaded == (plan.remote_dispatch_path,)
    assert len(runner.calls) == 1
    assert "snapshot: local payload file not found" in result.errors[0]


def test_scp_payload_uploader_redacts_token_shaped_stderr(tmp_path: Path):
    """Test token-shaped stderr values are redacted from upload errors."""
    config, plan = _materialized_plan(tmp_path)
    token_shaped = "ghp_abcdefghijklmnopqrstuvwxyz123456"
    runner = FakeUploadRunner(returncodes=[1, 0], stderrs=[f"auth failed {token_shaped}", ""])
    uploader = SCPPayloadUploader(
        host=config.remote.host or "",
        runner=runner,
        extra_secrets=(config.tracker.token,),
    )

    result = uploader.upload(plan)

    assert not result.ok
    assert result.partial
    assert token_shaped not in " ".join(result.errors)
    assert REDACTED in " ".join(result.errors)
    assert result.uploaded == (plan.remote_dispatch_path,)


def test_scp_payload_uploader_redacts_explicit_secret_from_oserror(tmp_path: Path):
    """Test explicit coordinator secrets are redacted from subprocess errors."""
    config, plan = _materialized_plan(tmp_path)
    runner = FakeUploadRunner(raise_errors=[OSError(f"bad token {config.tracker.token}"), None])
    uploader = SCPPayloadUploader(
        host=config.remote.host or "",
        runner=runner,
        extra_secrets=(config.tracker.token,),
    )

    result = uploader.upload(plan)

    assert not result.ok
    assert config.tracker.token not in " ".join(result.errors)
    assert REDACTED in " ".join(result.errors)


def test_scp_payload_uploader_timeout_is_structured_error(tmp_path: Path):
    """Test subprocess timeouts become structured upload errors."""
    config, plan = _materialized_plan(tmp_path)
    runner = FakeUploadRunner(
        raise_errors=[subprocess.TimeoutExpired(["scp"], timeout=12.0), None]
    )
    uploader = SCPPayloadUploader(
        host=config.remote.host or "",
        runner=runner,
        timeout_seconds=12.0,
    )

    result = uploader.upload(plan)

    assert not result.ok
    assert result.partial
    assert "snapshot: scp timed out after 12.0s" in result.errors
