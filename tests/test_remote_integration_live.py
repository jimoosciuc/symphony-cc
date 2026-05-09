"""Opt-in live integration test for remote execution.

Skipped by default. Enabled when ALL of:

- ``SYMPHONY_RUN_REMOTE_INTEGRATION=1``
- ``SYMPHONY_REMOTE_TEST_HOST`` is an SSH target reachable from this machine
- ``SYMPHONY_REMOTE_WORKSPACE_ROOT`` is an absolute writable path on that host
- ``SYMPHONY_REMOTE_ARTIFACT_ROOT`` is an absolute writable path on that host
- ``SYMPHONY_REMOTE_SESSION_STORE`` is an absolute writable path on that host
- ``ssh`` and ``scp`` are available locally
- ``symphony-worker`` is installed on the remote host's ``PATH``

Optional:

- ``SYMPHONY_REMOTE_GIT_TOKEN`` supplies the trusted-host git credential field
  in the remote snapshot. The smoke test runs ``symphony-worker --fake`` and
  does not perform git network operations.

This file intentionally exercises only the remote transport harness: stage a
materialized snapshot + dispatch request, invoke ``symphony-worker --fake`` via
SSH, and parse worker protocol events. Real Claude execution remains a separate
opt-in E2E because it depends on remote Claude CLI auth and is more expensive.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from symphony.config import build_config
from symphony.models import Issue
from symphony.remote.materialize import materialize_remote_dispatch_plan
from symphony.remote.plan import build_remote_dispatch_plan
from symphony.remote.protocol import parse_worker_event

_GATE_ENV = "SYMPHONY_RUN_REMOTE_INTEGRATION"


def _gate() -> dict[str, str]:
    if os.environ.get(_GATE_ENV) != "1":
        pytest.skip(f"{_GATE_ENV} not set; live remote tests skipped")
    if shutil.which("ssh") is None:
        pytest.skip("`ssh` not on PATH; live remote tests skipped")
    if shutil.which("scp") is None:
        pytest.skip("`scp` not on PATH; live remote tests skipped")

    required = {
        "host": "SYMPHONY_REMOTE_TEST_HOST",
        "workspace_root": "SYMPHONY_REMOTE_WORKSPACE_ROOT",
        "artifact_root": "SYMPHONY_REMOTE_ARTIFACT_ROOT",
        "session_store": "SYMPHONY_REMOTE_SESSION_STORE",
    }
    values: dict[str, str] = {}
    missing: list[str] = []
    for key, env_name in required.items():
        value = os.environ.get(env_name)
        if not value:
            missing.append(env_name)
            continue
        values[key] = value

    if missing:
        pytest.skip(f"{', '.join(missing)} not set; live remote tests skipped")
    return values


def _issue() -> Issue:
    return Issue(
        id="remote-live#987654",
        number=987654,
        identifier="symphony-cc/remote-live#987654",
        owner="symphony-cc",
        repo="remote-live",
        title="Remote live smoke",
        body="Remote live smoke",
        state="open",
        url="https://github.com/symphony-cc/remote-live/issues/987654",
    )


def _config(tmp_path: Path, remote_env: dict[str, str]):
    remote = {
        "enabled": True,
        "host": remote_env["host"],
        "workspace_root": remote_env["workspace_root"],
        "artifact_root": remote_env["artifact_root"],
        "session_store": remote_env["session_store"],
        "worker_timeout_ms": 60_000,
        "stall_timeout_ms": 30_000,
    }
    if os.environ.get("SYMPHONY_REMOTE_GIT_TOKEN"):
        remote["git_token"] = os.environ["SYMPHONY_REMOTE_GIT_TOKEN"]

    return build_config(
        {
            "tracker": {
                "kind": "github",
                "owner": "symphony-cc",
                "repo": "remote-live",
                "token": "live-remote-placeholder-token",
            },
            "agent": {"provider": "claude_code"},
            "workspace": {"root": str(tmp_path / "workspaces")},
            "claude": {
                "model": "claude-live-remote-smoke",
                "permission_mode": "acceptEdits",
                "session_store": str(tmp_path / "sessions"),
                "transcript_store": str(tmp_path / "transcripts"),
                "artifact_store": str(tmp_path / "artifacts"),
            },
            "github": {},
            "remote": remote,
        },
        workflow_path=tmp_path / "WORKFLOW.md",
    )


def _run(args: list[str], *, timeout: float = 30.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def test_live_remote_worker_fake_smoke_over_ssh(tmp_path: Path) -> None:
    remote_env = _gate()
    config = _config(tmp_path, remote_env)
    issue = _issue()
    plan = build_remote_dispatch_plan(issue, attempt=1, config=config)
    materialize_remote_dispatch_plan(plan, config)

    host = remote_env["host"]
    remote_payload_dir = str(Path(plan.remote_snapshot_path).parent)

    mkdir = _run(
        [
            "ssh",
            host,
            "mkdir",
            "-p",
            remote_payload_dir,
            plan.remote_artifact_path,
            plan.dispatch_request.workspace_path,
        ]
    )
    assert mkdir.returncode == 0, _safe_failure("remote mkdir failed", mkdir)

    try:
        for local_path, remote_path in (
            (plan.local_snapshot_path, plan.remote_snapshot_path),
            (plan.local_dispatch_path, plan.remote_dispatch_path),
        ):
            upload = _run(["scp", "-q", str(local_path), f"{host}:{remote_path}"])
            assert upload.returncode == 0, _safe_failure("remote payload upload failed", upload)

        worker = _run(
            [
                "ssh",
                host,
                "symphony-worker",
                "--snapshot-path",
                plan.remote_snapshot_path,
                "--dispatch-path",
                plan.remote_dispatch_path,
                "--fake",
            ],
            timeout=60.0,
        )

        assert worker.returncode == 0, _safe_failure("remote worker failed", worker)
        events = [parse_worker_event(line) for line in worker.stdout.splitlines() if line]
        event_names = [event.event for event in events]
        assert event_names == [
            "worker_started",
            "workspace_ready",
            "session_started",
            "heartbeat",
            "worker_completed",
        ]
        assert all(event.issue_identifier == issue.identifier for event in events)
        assert all(event.attempt == 1 for event in events)
        assert config.tracker.token not in worker.stdout
        git_token = os.environ.get("SYMPHONY_REMOTE_GIT_TOKEN")
        if git_token:
            assert git_token not in worker.stdout
    finally:
        _run(
            [
                "ssh",
                host,
                "rm",
                "-rf",
                plan.remote_workspace_path,
                plan.remote_artifact_path,
            ],
            timeout=30.0,
        )


def _safe_failure(prefix: str, result: subprocess.CompletedProcess) -> str:
    detail = "\n".join(part for part in (result.stderr.strip(), result.stdout.strip()) if part)
    token = os.environ.get("SYMPHONY_REMOTE_GIT_TOKEN")
    if token:
        detail = detail.replace(token, "<redacted>")
    return f"{prefix}: exit={result.returncode} {detail}".strip()
