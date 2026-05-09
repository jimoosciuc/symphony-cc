"""Opt-in remote real-Claude E2E harness (#166 / M9.2).

Skipped by default. Enabled when ALL of:

- ``SYMPHONY_RUN_REMOTE_CLAUDE_E2E=1``
- ``GITHUB_TOKEN`` is non-empty for read-side PR evidence lookup
- ``SYMPHONY_REMOTE_TEST_HOST`` is an SSH target reachable from this machine
- ``SYMPHONY_REMOTE_WORKSPACE_ROOT`` is an absolute writable path on that host
- ``SYMPHONY_REMOTE_ARTIFACT_ROOT`` is an absolute writable path on that host
- ``SYMPHONY_REMOTE_SESSION_STORE`` is an absolute writable path on that host
- ``SYMPHONY_REMOTE_GIT_TOKEN`` is a git-only credential for remote checkout
- ``ssh`` and ``scp`` are available locally
- ``symphony-worker`` and authenticated ``claude`` are installed on the remote host

The default CI test in this module does not touch SSH or Claude; it only
guards harness assembly and redaction boundaries so the live path cannot rot
behind a skip gate.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

from symphony.config import build_config
from symphony.evidence import EvidenceDetector
from symphony.github.client import GitHubClient
from symphony.models import Issue
from symphony.provider.base import Terminal
from symphony.remote.materialize import materialize_remote_dispatch_plan
from symphony.remote.plan import build_remote_dispatch_plan
from symphony.remote.protocol import parse_worker_event
from symphony.remote.snapshot import REMOTE_TRACKER_TOKEN_PLACEHOLDER

_GATE_ENV = "SYMPHONY_RUN_REMOTE_CLAUDE_E2E"
_DEFAULT_MODEL = "claude-opus-4-7"
_REQUIRE_PR_ENV = "SYMPHONY_REMOTE_CLAUDE_REQUIRE_PR"
_PR_DETECT_ATTEMPTS_ENV = "SYMPHONY_REMOTE_CLAUDE_PR_DETECT_ATTEMPTS"
_PR_DETECT_INTERVAL_ENV = "SYMPHONY_REMOTE_CLAUDE_PR_DETECT_INTERVAL_SECONDS"


def _gate() -> dict[str, str]:
    if os.environ.get(_GATE_ENV) != "1":
        pytest.skip(f"{_GATE_ENV} not set; remote Claude E2E skipped")
    if shutil.which("ssh") is None:
        pytest.skip("`ssh` not on PATH; remote Claude E2E skipped")
    if shutil.which("scp") is None:
        pytest.skip("`scp` not on PATH; remote Claude E2E skipped")
    if not os.environ.get("GITHUB_TOKEN"):
        pytest.skip("GITHUB_TOKEN not set; remote Claude E2E skipped")

    required = {
        "host": "SYMPHONY_REMOTE_TEST_HOST",
        "workspace_root": "SYMPHONY_REMOTE_WORKSPACE_ROOT",
        "artifact_root": "SYMPHONY_REMOTE_ARTIFACT_ROOT",
        "session_store": "SYMPHONY_REMOTE_SESSION_STORE",
        "git_token": "SYMPHONY_REMOTE_GIT_TOKEN",
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
        pytest.skip(f"{', '.join(missing)} not set; remote Claude E2E skipped")
    return values


def _issue() -> Issue:
    number = int(os.environ.get("SYMPHONY_REMOTE_CLAUDE_TEST_ISSUE", "987655"))
    owner = os.environ.get("SYMPHONY_GITHUB_TEST_OWNER", "jimoosciuc")
    repo = os.environ.get("SYMPHONY_GITHUB_TEST_REPO", "symphony-cc")
    return Issue(
        id=f"remote-claude-live#{number}",
        number=number,
        identifier=f"{owner}/{repo}#{number}",
        owner=owner,
        repo=repo,
        title="Remote Claude live E2E smoke",
        body=(
            "Remote Claude live E2E smoke. Reply with a concise completion; "
            "do not make unrelated changes."
        ),
        state="open",
        url=f"https://github.com/{owner}/{repo}/issues/{number}",
    )


def _config(tmp_path: Path, remote_env: dict[str, str]):
    owner = os.environ.get("SYMPHONY_GITHUB_TEST_OWNER", "jimoosciuc")
    repo = os.environ.get("SYMPHONY_GITHUB_TEST_REPO", "symphony-cc")
    return build_config(
        {
            "tracker": {
                "kind": "github",
                "owner": owner,
                "repo": repo,
                # Coordinator tracker auth must not be serialized to the worker.
                "token": "coordinator-tracker-token",
            },
            "agent": {"provider": "claude_code"},
            "workspace": {"root": str(tmp_path / "workspaces")},
            "claude": {
                "model": os.environ.get("SYMPHONY_CLAUDE_TEST_MODEL", _DEFAULT_MODEL),
                "permission_mode": os.environ.get(
                    "SYMPHONY_REMOTE_CLAUDE_PERMISSION_MODE",
                    "bypassPermissions",
                ),
                "session_store": remote_env["session_store"],
                "transcript_store": str(tmp_path / "transcripts"),
                "artifact_store": str(tmp_path / "artifacts"),
                "turn_timeout_ms": 300_000,
                "stall_timeout_ms": 120_000,
            },
            "github": {},
            "remote": {
                "enabled": True,
                "host": remote_env["host"],
                "workspace_root": remote_env["workspace_root"],
                "artifact_root": remote_env["artifact_root"],
                "session_store": remote_env["session_store"],
                "worker_timeout_ms": 420_000,
                "git_token": remote_env["git_token"],
            },
            "logging": {
                "redact_keys": [
                    "token",
                    "authorization",
                    "api_key",
                    "password",
                    "secret",
                    "git_token",
                ]
            },
        },
        workflow_path=tmp_path / "WORKFLOW.md",
    )


def _require_completed_with_pr() -> bool:
    """Return whether remote Claude E2E must prove linked PR evidence."""
    return os.environ.get(_REQUIRE_PR_ENV) == "1"


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        pytest.fail(f"{name}={raw!r} is not a valid integer")
    if value < 1:
        pytest.fail(f"{name} must be >= 1")
    return value


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        pytest.fail(f"{name}={raw!r} is not a valid number")
    if value < 0:
        pytest.fail(f"{name} must be >= 0")
    return value


def test_remote_claude_e2e_payload_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default-CI guard for the remote real-Claude harness assembly."""
    remote_env = {
        "host": "remote.example",
        "workspace_root": "/tmp/symphony-remote/workspaces",
        "artifact_root": "/tmp/symphony-remote/artifacts",
        "session_store": "/tmp/symphony-remote/sessions",
        "git_token": "remote-git-secret",
    }
    monkeypatch.delenv("SYMPHONY_CLAUDE_TEST_MODEL", raising=False)
    monkeypatch.delenv(_REQUIRE_PR_ENV, raising=False)
    config = _config(tmp_path, remote_env)
    issue = _issue()
    plan = build_remote_dispatch_plan(issue, attempt=1, config=config)
    materialized = materialize_remote_dispatch_plan(plan, config)

    snapshot = json.loads(materialized.snapshot_path.read_text(encoding="utf-8"))
    dispatch = json.loads(materialized.dispatch_path.read_text(encoding="utf-8"))

    assert snapshot["tracker"]["token"] == REMOTE_TRACKER_TOKEN_PLACEHOLDER
    assert "coordinator-tracker-token" not in materialized.snapshot_path.read_text(
        encoding="utf-8"
    )
    assert snapshot["remote"]["git_token"] == "remote-git-secret"
    assert "git_token" in snapshot["logging"]["redact_keys"]
    assert snapshot["claude"]["model"] == _DEFAULT_MODEL
    assert dispatch["workspace_path"].startswith(remote_env["workspace_root"])
    assert dispatch["artifact_path"].startswith(remote_env["artifact_root"])
    assert dispatch["branch"]
    assert plan.remote_snapshot_path.endswith("/snapshot.json")
    assert plan.remote_dispatch_path.endswith("/dispatch.json")
    assert _require_completed_with_pr() is False


def test_remote_claude_e2e_can_require_completed_with_pr(monkeypatch) -> None:
    monkeypatch.setenv(_REQUIRE_PR_ENV, "1")

    assert _require_completed_with_pr() is True


def test_remote_pr_required_assertion_accepts_linked_pr() -> None:
    _assert_required_pr(
        {
            "pr_created": True,
            "pr_number": 216,
            "pr_url": "https://github.com/jimoosciuc/symphony-cc/pull/216",
            "task_outcome": "completed_with_pr",
        }
    )


def test_remote_pr_required_assertion_fails_without_linked_pr() -> None:
    with pytest.raises(AssertionError, match="completed_with_pr"):
        _assert_required_pr(
            {
                "pr_created": False,
                "pr_number": None,
                "pr_url": None,
                "task_outcome": "incomplete_no_evidence",
            }
        )


def test_live_remote_claude_worker_over_ssh(tmp_path: Path) -> None:
    remote_env = _gate()
    config = _config(tmp_path, remote_env)
    issue = _issue()
    plan = build_remote_dispatch_plan(issue, attempt=1, config=config)
    materialize_remote_dispatch_plan(plan, config)

    host = remote_env["host"]
    remote_payload_dir = str(Path(plan.remote_snapshot_path).parent)
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()

    prereq = _run(
        [
            "ssh",
            host,
            "sh",
            "-lc",
            "command -v symphony-worker >/dev/null && command -v claude >/dev/null",
        ],
        timeout=30.0,
    )
    assert prereq.returncode == 0, _safe_failure(
        "remote missing symphony-worker or claude",
        prereq,
        config=config,
    )

    mkdir = _run(
        [
            "ssh",
            host,
            "mkdir",
            "-p",
            remote_payload_dir,
            plan.remote_artifact_path,
            plan.dispatch_request.workspace_path,
        ],
        timeout=30.0,
    )
    assert mkdir.returncode == 0, _safe_failure("remote mkdir failed", mkdir, config=config)

    try:
        for local_path, remote_path in (
            (plan.local_snapshot_path, plan.remote_snapshot_path),
            (plan.local_dispatch_path, plan.remote_dispatch_path),
        ):
            upload = _run(["scp", "-q", str(local_path), f"{host}:{remote_path}"])
            assert upload.returncode == 0, _safe_failure(
                "remote payload upload failed",
                upload,
                config=config,
            )

        worker = _run(
            [
                "ssh",
                host,
                "symphony-worker",
                "--snapshot-path",
                plan.remote_snapshot_path,
                "--dispatch-path",
                plan.remote_dispatch_path,
            ],
            timeout=config.remote.worker_timeout_ms / 1000.0 + 30.0,
        )
        assert config.remote.git_token not in worker.stdout
        assert config.remote.git_token not in worker.stderr

        events = [parse_worker_event(line) for line in worker.stdout.splitlines() if line]
        event_names = [event.event for event in events]
        assert "worker_started" in event_names
        assert "workspace_ready" in event_names
        assert "session_started" in event_names
        assert event_names[-1] in {"worker_completed", "worker_failed"}
        assert all(event.issue_identifier == issue.identifier for event in events)
        assert all(event.host == host for event in events)

        terminal = _fetch_remote_json(
            host,
            f"{plan.remote_artifact_path}/terminal.json",
            config=config,
        )
        assert terminal["execution"] == "remote"
        assert terminal["terminal_state"] in {"completed", "failed", "cancelled", "crashed"}

        pr_summary = _lookup_pr_summary_with_optional_retry(
            issue,
            config=config,
            require_completed_with_pr=_require_completed_with_pr(),
        )
        evidence = {
            "issue_identifier": issue.identifier,
            "remote_host": host,
            "remote_workspace_path": plan.remote_workspace_path,
            "remote_artifact_path": plan.remote_artifact_path,
            "events": event_names,
            "terminal": terminal,
            "provider_session_id": _provider_session_id(events),
            "require_completed_with_pr": _require_completed_with_pr(),
            "pr_detect_attempts": pr_summary["attempts"],
            "pr_detect_wait_seconds": pr_summary["wait_seconds"],
            "pr_created": pr_summary["pr_created"],
            "pr_number": pr_summary["pr_number"],
            "pr_url": pr_summary["pr_url"],
        }
        evidence_file = evidence_dir / f"remote_claude_e2e_issue_{issue.number}.json"
        evidence_file.write_text(json.dumps(evidence, indent=2, sort_keys=True))

        assert worker.returncode == 0, _safe_failure(
            "remote real-Claude worker failed",
            worker,
            config=config,
        )
        if _require_completed_with_pr():
            _assert_required_pr(pr_summary)
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


def _lookup_pr_summary_with_optional_retry(
    issue: Issue,
    *,
    config,
    require_completed_with_pr: bool,
    sleep=time.sleep,
) -> dict[str, Any]:
    attempts = _env_int(_PR_DETECT_ATTEMPTS_ENV, 12) if require_completed_with_pr else 1
    interval_seconds = (
        _env_float(_PR_DETECT_INTERVAL_ENV, 5.0) if require_completed_with_pr else 0.0
    )
    summary: dict[str, Any] | None = None
    for attempt in range(1, attempts + 1):
        summary = _lookup_pr_summary(issue, config=config)
        summary["attempts"] = attempt
        summary["wait_seconds"] = interval_seconds * (attempt - 1)
        if summary["task_outcome"] == "completed_with_pr" or attempt == attempts:
            return summary
        sleep(interval_seconds)
    raise AssertionError("unreachable remote PR retry loop")


def _lookup_pr_summary(issue: Issue, *, config) -> dict[str, Any]:
    client = GitHubClient(os.environ["GITHUB_TOKEN"])
    try:
        detector = EvidenceDetector(config.github, client=client)
        result = detector.detect(
            issue=issue,
            terminal_state=Terminal.COMPLETED,
            retryable=False,
            blocked=False,
            permission_denials_count=0,
            last_event=None,
            recent_assistant_text="",
            workspace_path=None,
        )
    finally:
        client.close()
    for entry in result.task_evidence:
        if entry.get("type") == "pr_linked":
            return {
                "task_outcome": result.task_outcome,
                "pr_created": True,
                "pr_number": entry.get("number"),
                "pr_url": entry.get("url"),
            }
    return {
        "task_outcome": result.task_outcome,
        "pr_created": False,
        "pr_number": None,
        "pr_url": None,
    }


def _assert_required_pr(summary: dict[str, Any]) -> None:
    assert (
        summary.get("task_outcome") == "completed_with_pr"
        and summary.get("pr_created") is True
        and summary.get("pr_number")
        and summary.get("pr_url")
    ), f"remote Claude E2E required completed_with_pr; pr_summary={summary}"


def _provider_session_id(events) -> str | None:
    for event in events:
        if event.event == "session_started":
            value = event.fields.get("provider_session_id")
            return value if isinstance(value, str) else None
    return None


def _fetch_remote_json(host: str, path: str, *, config) -> dict[str, Any]:
    result = _run(["ssh", host, "cat", path], timeout=30.0)
    assert result.returncode == 0, _safe_failure(
        f"remote cat failed for {path}",
        result,
        config=config,
    )
    try:
        parsed = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise AssertionError(f"remote JSON was malformed: {exc}") from exc
    assert isinstance(parsed, dict)
    return parsed


def _run(args: list[str], *, timeout: float = 30.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _safe_failure(prefix: str, result: subprocess.CompletedProcess, *, config) -> str:
    detail = "\n".join(part for part in (result.stderr.strip(), result.stdout.strip()) if part)
    secrets = [
        config.tracker.token,
        config.remote.git_token or "",
        os.environ.get("GITHUB_TOKEN", ""),
    ]
    for secret in secrets:
        if secret:
            detail = detail.replace(secret, "<redacted>")
    return f"{prefix}: exit={result.returncode} {detail}".strip()
