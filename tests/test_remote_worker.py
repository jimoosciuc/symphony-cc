"""Tests for symphony-worker CLI stub."""

import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

from symphony.artifacts import REDACTED
from symphony.config import build_config
from symphony.provider.fake import FakeProvider, FakeTurnScript
from symphony.remote.dispatch import DispatchRequest
from symphony.remote.protocol import parse_worker_event
from symphony.remote.worker import (
    _redact_error_message,
    build_worker_workspace_populator,
    run_real_worker,
)


def _minimal_snapshot() -> dict:
    """Return minimal valid config snapshot."""
    return {
        "tracker": {
            "kind": "github",
            "owner": "test-owner",
            "repo": "test-repo",
            "token": "test-token-12345",
        },
        "agent": {"provider": "claude_code"},
        "workspace": {"root": "/tmp/workspaces"},
        "claude": {
            "model": "claude-fake",
            "permission_mode": "acceptEdits",
            "session_store": "/tmp/sessions",
            "transcript_store": "/tmp/transcripts",
            "artifact_store": "/tmp/artifacts",
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


def _real_worker_config(tmp_path: Path, *, git_token: str | None = None):
    snapshot = _minimal_snapshot()
    snapshot["tracker"]["token"] = "remote-worker-no-tracker-token"
    snapshot["workspace"]["root"] = str(tmp_path / "remote" / "workspaces")
    snapshot["claude"]["session_store"] = str(tmp_path / "remote" / "sessions")
    snapshot["claude"]["transcript_store"] = str(tmp_path / "remote" / "transcripts")
    snapshot["claude"]["artifact_store"] = str(tmp_path / "remote" / "artifacts")
    snapshot["remote"]["workspace_root"] = str(tmp_path / "remote" / "workspaces")
    snapshot["remote"]["artifact_root"] = str(tmp_path / "remote" / "artifacts")
    snapshot["remote"]["session_store"] = str(tmp_path / "remote" / "sessions")
    if git_token is not None:
        snapshot["remote"]["git_token"] = git_token
    return build_config(snapshot, workflow_path=tmp_path / "WORKFLOW.md")


def _dispatch(tmp_path: Path, *, workspace_path: Path | None = None) -> DispatchRequest:
    workspace = workspace_path or (
        tmp_path / "remote" / "workspaces" / "test-owner" / "test-repo" / "42"
    )
    return DispatchRequest(
        owner="test-owner",
        repo="test-repo",
        issue_number=42,
        attempt=2,
        workspace_path=str(workspace),
        artifact_path=str(tmp_path / "remote" / "artifacts" / "test-owner_test-repo_42" / "2"),
        branch="symphony/42-test-issue",
        base_branch="main",
    )


class FakePopulator:
    def __init__(self, *, fail_with: str | None = None) -> None:
        self.fail_with = fail_with
        self.calls: list[tuple[Path, str, bool]] = []

    def populate(self, workspace_path: Path, issue, *, reused: bool) -> None:
        self.calls.append((workspace_path, issue.identifier, reused))
        if self.fail_with:
            raise RuntimeError(self.fail_with)


def test_worker_cli_help():
    """Test symphony-worker --help works."""
    result = subprocess.run(
        [sys.executable, "-m", "symphony.remote.worker", "--help"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "symphony-worker" in result.stdout
    assert "--snapshot-path" in result.stdout
    assert "--fake" in result.stdout


def test_worker_rejects_missing_snapshot(tmp_path: Path):
    """Test worker rejects missing snapshot file."""
    snapshot_path = tmp_path / "missing.json"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "symphony.remote.worker",
            "--snapshot-path",
            str(snapshot_path),
            "--fake",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "not found" in result.stderr.lower()


def test_worker_rejects_malformed_json(tmp_path: Path):
    """Test worker rejects malformed JSON snapshot."""
    snapshot_path = tmp_path / "malformed.json"
    snapshot_path.write_text("{invalid json", encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "symphony.remote.worker",
            "--snapshot-path",
            str(snapshot_path),
            "--fake",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "malformed" in result.stderr.lower() or "json" in result.stderr.lower()


def test_worker_rejects_invalid_snapshot(tmp_path: Path):
    """Test worker rejects snapshot with missing required fields."""
    snapshot_path = tmp_path / "invalid.json"
    snapshot = _minimal_snapshot()
    del snapshot["remote"]["host"]
    snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "symphony.remote.worker",
            "--snapshot-path",
            str(snapshot_path),
            "--fake",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "host" in result.stderr.lower()


def test_worker_rejects_disabled_remote(tmp_path: Path):
    """Test worker rejects snapshot with remote.enabled=false."""
    snapshot_path = tmp_path / "disabled.json"
    snapshot = _minimal_snapshot()
    snapshot["remote"]["enabled"] = False
    snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "symphony.remote.worker",
            "--snapshot-path",
            str(snapshot_path),
            "--fake",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "enabled" in result.stderr.lower()


def test_worker_fake_mode_emits_valid_protocol_events(tmp_path: Path):
    """Test worker --fake emits valid protocol events."""
    snapshot_path = tmp_path / "valid.json"
    snapshot = _minimal_snapshot()
    snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")

    # Create dispatch request
    dispatch_path = tmp_path / "dispatch.json"
    dispatch_path.write_text(
        json.dumps(
            {
                "owner": "test-owner",
                "repo": "test-repo",
                "issue_number": 42,
                "attempt": 2,
                "workspace_path": "/remote/workspaces/test-owner_test-repo_42",
                "artifact_path": "/remote/artifacts/test-owner_test-repo_42/2",
            }
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "symphony.remote.worker",
            "--snapshot-path",
            str(snapshot_path),
            "--dispatch-path",
            str(dispatch_path),
            "--fake",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0

    # Parse all emitted events
    lines = result.stdout.strip().split("\n")
    assert len(lines) >= 5  # At least 5 events

    events = []
    for line in lines:
        event = parse_worker_event(line)
        events.append(event)

    # Check event sequence
    assert events[0].event == "worker_started"
    assert events[1].event == "workspace_ready"
    assert events[2].event == "session_started"
    assert events[3].event == "heartbeat"
    assert events[4].event == "worker_completed"

    # Check that events use dispatch request fields
    for event in events:
        assert event.timestamp
        assert event.issue_identifier == "test-owner/test-repo#42"
        assert event.attempt == 2
        assert event.host

    # Check workspace_ready uses dispatch workspace_path
    assert events[1].fields["workspace_path"] == "/remote/workspaces/test-owner_test-repo_42"

    # Check worker_completed uses dispatch artifact_path
    assert events[4].fields["artifact_path"] == "/remote/artifacts/test-owner_test-repo_42/2"


def test_worker_fake_mode_requires_dispatch_path(tmp_path: Path):
    """Test worker --fake requires --dispatch-path."""
    snapshot_path = tmp_path / "valid.json"
    snapshot = _minimal_snapshot()
    snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "symphony.remote.worker",
            "--snapshot-path",
            str(snapshot_path),
            "--fake",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "dispatch-path required" in result.stderr.lower()


def test_worker_real_mode_requires_dispatch_path(tmp_path: Path):
    """Test non-fake worker mode requires --dispatch-path."""
    snapshot_path = tmp_path / "valid.json"
    snapshot = _minimal_snapshot()
    snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "symphony.remote.worker",
            "--snapshot-path",
            str(snapshot_path),
        ],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "dispatch-path required" in result.stderr.lower()


async def test_real_worker_prepares_dispatch_workspace_and_artifacts(tmp_path: Path):
    """Real worker path prepares dispatch workspace and writes artifacts."""
    config = _real_worker_config(tmp_path)
    dispatch = _dispatch(tmp_path)
    provider = FakeProvider()
    lines: list[str] = []

    code = await run_real_worker(
        config,
        dispatch,
        provider_factory=lambda config: provider,
        workspace_populator=FakePopulator(),
        emit=lines.append,
    )

    assert code == 0
    assert Path(dispatch.workspace_path).is_dir()
    artifact_root = Path(dispatch.artifact_path)
    assert (artifact_root / "request.json").exists()
    assert (artifact_root / "session.json").exists()
    assert (artifact_root / "events.jsonl").exists()
    terminal = json.loads((artifact_root / "terminal.json").read_text())
    assert terminal["terminal_state"] == "completed"

    events = [parse_worker_event(line) for line in lines]
    assert [event.event for event in events] == [
        "worker_started",
        "workspace_ready",
        "session_started",
        "heartbeat",
        "turn_completed",
        "worker_completed",
    ]
    assert events[1].fields["workspace_path"] == dispatch.workspace_path
    assert events[-1].fields["artifact_path"] == dispatch.artifact_path


async def test_real_worker_runs_workspace_hooks_with_workspace_cwd(tmp_path: Path):
    """Real worker runs workspace lifecycle hooks in the dispatch workspace."""
    config = _real_worker_config(tmp_path)
    after_create = tmp_path / "after_create.txt"
    before_run = tmp_path / "before_run.txt"
    after_run = tmp_path / "after_run.txt"
    config = replace(
        config,
        workspace=replace(
            config.workspace,
            after_create=f"pwd >> {after_create}",
            before_run=f"pwd >> {before_run}",
            after_run=f"pwd >> {after_run}",
        ),
    )
    dispatch = _dispatch(tmp_path)

    code = await run_real_worker(
        config,
        dispatch,
        provider_factory=lambda config: FakeProvider(),
        workspace_populator=FakePopulator(),
        emit=lambda line: None,
    )

    assert code == 0
    assert after_create.read_text().strip() == dispatch.workspace_path
    assert before_run.read_text().strip() == dispatch.workspace_path
    assert after_run.read_text().strip() == dispatch.workspace_path


async def test_real_worker_after_create_only_runs_for_fresh_workspace(tmp_path: Path):
    """after_create runs only for a fresh remote workspace."""
    config = _real_worker_config(tmp_path)
    after_create = tmp_path / "after_create.txt"
    config = replace(
        config,
        workspace=replace(config.workspace, after_create=f"echo created >> {after_create}"),
    )
    dispatch = _dispatch(tmp_path)

    for _ in range(2):
        code = await run_real_worker(
            config,
            dispatch,
            provider_factory=lambda config: FakeProvider(),
            workspace_populator=FakePopulator(),
            emit=lambda line: None,
        )
        assert code == 0

    assert after_create.read_text().splitlines() == ["created"]


async def test_real_worker_calls_fake_populator_for_git_population(tmp_path: Path):
    """Injected populator runs for workspace.populate=git without network access."""
    config = _real_worker_config(tmp_path)
    config = replace(config, workspace=replace(config.workspace, populate="git"))
    dispatch = _dispatch(tmp_path)
    populator = FakePopulator()

    code = await run_real_worker(
        config,
        dispatch,
        provider_factory=lambda config: FakeProvider(),
        workspace_populator=populator,
        emit=lambda line: None,
    )

    assert code == 0
    assert populator.calls == [
        (Path(dispatch.workspace_path), dispatch.issue_identifier, False)
    ]


def test_worker_git_populator_uses_remote_git_token(tmp_path: Path):
    """Worker-side git populator consumes the narrow remote git credential."""
    config = _real_worker_config(tmp_path, git_token="git-only-token")
    config = replace(config, workspace=replace(config.workspace, populate="git"))

    populator = build_worker_workspace_populator(config)

    assert populator is not None
    assert populator._tracker.token == "git-only-token"
    assert populator._tracker.token != config.tracker.token


async def test_real_worker_missing_git_token_fails_before_provider(tmp_path: Path):
    """workspace.populate=git without git token fails before provider execution."""
    config = _real_worker_config(tmp_path, git_token=None)
    config = replace(config, workspace=replace(config.workspace, populate="git"))
    dispatch = _dispatch(tmp_path)
    provider = FakeProvider()
    lines: list[str] = []

    code = await run_real_worker(
        config,
        dispatch,
        provider_factory=lambda config: provider,
        emit=lines.append,
    )

    assert code == 1
    assert provider.calls == []
    events = [parse_worker_event(line) for line in lines]
    assert events[-1].event == "worker_failed"
    assert "remote.git_token is required" in events[-1].fields["message"]


async def test_real_worker_populator_failure_is_failed_terminal(tmp_path: Path):
    """Populator failure emits worker_failed and writes failed terminal."""
    config = _real_worker_config(tmp_path, git_token="plain-git-secret")
    config = replace(config, workspace=replace(config.workspace, populate="git"))
    dispatch = _dispatch(tmp_path)
    populator = FakePopulator(fail_with=f"clone failed {config.remote.git_token}")
    lines: list[str] = []

    code = await run_real_worker(
        config,
        dispatch,
        provider_factory=lambda config: FakeProvider(),
        workspace_populator=populator,
        emit=lines.append,
    )

    assert code == 1
    events = [parse_worker_event(line) for line in lines]
    assert events[-1].event == "worker_failed"
    assert config.remote.git_token not in events[-1].fields["message"]
    terminal = json.loads((Path(dispatch.artifact_path) / "terminal.json").read_text())
    assert terminal["terminal_state"] == "failed"
    assert config.remote.git_token not in terminal["error"]


async def test_real_worker_hook_failure_is_failed_terminal(tmp_path: Path):
    """Hook failure is explicit and does not count as worker success."""
    config = _real_worker_config(tmp_path)
    config = replace(config, workspace=replace(config.workspace, before_run="exit 7"))
    dispatch = _dispatch(tmp_path)
    lines: list[str] = []

    code = await run_real_worker(
        config,
        dispatch,
        provider_factory=lambda config: FakeProvider(),
        workspace_populator=FakePopulator(),
        emit=lines.append,
    )

    assert code == 1
    events = [parse_worker_event(line) for line in lines]
    assert events[-1].event == "worker_failed"
    assert "before_run hook failed" in events[-1].fields["message"]
    terminal = json.loads((Path(dispatch.artifact_path) / "terminal.json").read_text())
    assert terminal["terminal_state"] == "failed"


async def test_real_worker_rejects_workspace_outside_remote_root(tmp_path: Path):
    """Real worker path rejects dispatch workspace outside remote root."""
    config = _real_worker_config(tmp_path)
    dispatch = _dispatch(tmp_path, workspace_path=tmp_path / "outside" / "workspace")
    lines: list[str] = []

    code = await run_real_worker(
        config,
        dispatch,
        provider_factory=lambda config: FakeProvider(),
        workspace_populator=FakePopulator(),
        emit=lines.append,
    )

    assert code == 1
    events = [parse_worker_event(line) for line in lines]
    assert events[-1].event == "worker_failed"
    assert "inside remote.workspace_root" in events[-1].fields["message"]


async def test_real_worker_provider_failure_writes_failed_terminal(tmp_path: Path):
    """Provider failure emits worker_failed, returns non-zero, and writes terminal."""
    config = _real_worker_config(tmp_path)
    dispatch = _dispatch(tmp_path)
    provider = FakeProvider(
        default_script=FakeTurnScript(
            raise_after=0,
            raise_message="provider boom",
        )
    )
    lines: list[str] = []

    code = await run_real_worker(
        config,
        dispatch,
        provider_factory=lambda config: provider,
        workspace_populator=FakePopulator(),
        emit=lines.append,
    )

    assert code == 1
    events = [parse_worker_event(line) for line in lines]
    assert events[-1].event == "worker_failed"
    assert events[-1].fields["message"] == "provider boom"
    terminal = json.loads((Path(dispatch.artifact_path) / "terminal.json").read_text())
    assert terminal["terminal_state"] == "failed"
    assert terminal["error"] == "provider boom"


async def test_real_worker_redacts_tracker_placeholder_from_errors_and_artifacts(
    tmp_path: Path,
):
    """Worker errors redact tracker placeholder from streams and artifacts."""
    config = _real_worker_config(tmp_path)
    dispatch = _dispatch(tmp_path)
    provider = FakeProvider(
        default_script=FakeTurnScript(
            raise_after=0,
            raise_message=f"failed with {config.tracker.token}",
        )
    )
    lines: list[str] = []

    code = await run_real_worker(
        config,
        dispatch,
        provider_factory=lambda config: provider,
        workspace_populator=FakePopulator(),
        emit=lines.append,
    )

    assert code == 1
    stream = "\n".join(lines)
    assert config.tracker.token not in stream
    assert REDACTED in stream
    artifact_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in Path(dispatch.artifact_path).glob("*")
        if path.is_file()
    )
    assert config.tracker.token not in artifact_text
    assert REDACTED in artifact_text


def test_worker_dispatch_request_error_redaction(tmp_path: Path):
    """Test worker redacts secrets from dispatch request errors."""
    snapshot_path = tmp_path / "valid.json"
    snapshot = _minimal_snapshot()
    snapshot["tracker"]["token"] = "ghp_secret_token_12345"
    snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")

    # Create malformed dispatch request
    dispatch_path = tmp_path / "malformed.json"
    dispatch_path.write_text("{invalid json", encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "symphony.remote.worker",
            "--snapshot-path",
            str(snapshot_path),
            "--dispatch-path",
            str(dispatch_path),
            "--fake",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    # Token should not appear in stderr
    assert "ghp_secret_token_12345" not in result.stderr


def test_worker_dispatch_path_error_redacts_token_shaped_path(tmp_path: Path):
    """Test worker redacts token-shaped values from dispatch path errors."""
    snapshot_path = tmp_path / "valid.json"
    snapshot = _minimal_snapshot()
    snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")
    token_shaped = "ghp_abcdefghijklmnopqrstuvwxyz123456"
    dispatch_path = tmp_path / token_shaped

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "symphony.remote.worker",
            "--snapshot-path",
            str(snapshot_path),
            "--dispatch-path",
            str(dispatch_path),
            "--fake",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert token_shaped not in result.stderr
    assert REDACTED in result.stderr


def test_worker_error_output_redacts_tokens(tmp_path: Path):
    """Test worker redacts token-looking values from error output."""
    snapshot_path = tmp_path / "with-token.json"
    snapshot = _minimal_snapshot()
    snapshot["tracker"]["token"] = "ghp_secret123456789"
    # Make it invalid to trigger error
    del snapshot["remote"]["host"]
    snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "symphony.remote.worker",
            "--snapshot-path",
            str(snapshot_path),
            "--fake",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    # Token should not appear in stderr
    assert "ghp_secret123456789" not in result.stderr
    assert "secret" not in result.stderr.lower()


def test_worker_error_output_redacts_token_shaped_non_tracker_value(tmp_path: Path):
    """Worker stderr redacts token-shaped values even outside tracker.token."""
    snapshot_path = tmp_path / "token-shaped-permission.json"
    token_shaped_value = "ghp_abcdefghijklmnopqrstuvwxyz123456"
    snapshot = _minimal_snapshot()
    snapshot["claude"]["permission_mode"] = token_shaped_value
    snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "symphony.remote.worker",
            "--snapshot-path",
            str(snapshot_path),
            "--fake",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert token_shaped_value not in result.stderr
    assert REDACTED in result.stderr


def test_worker_error_redaction_uses_snapshot_secrets_before_config_exists():
    """Failure-path redaction works before WorkflowConfig is available."""
    raw_snapshot = {"tracker": {"token": "plain-secret-token"}}
    message = "snapshot validation failed for plain-secret-token"
    redacted = _redact_error_message(message, raw_snapshot)
    assert "plain-secret-token" not in redacted
    assert REDACTED in redacted


def test_worker_error_redaction_respects_snapshot_redact_keys():
    """Custom redact keys are honored for free-form worker errors."""
    raw_snapshot = {
        "logging": {"redact_keys": ["private_value"]},
        "remote": {"private_value": "custom-secret-value"},
    }
    message = "remote setup failed with custom-secret-value"
    redacted = _redact_error_message(message, raw_snapshot)
    assert "custom-secret-value" not in redacted
    assert REDACTED in redacted


def test_worker_has_no_tracker_api_dependency():
    """Test worker module does not import tracker client."""
    # This test verifies the worker can be imported without tracker dependencies
    from symphony.remote import worker

    # Worker should only depend on config and protocol
    assert hasattr(worker, "main")
    assert hasattr(worker, "load_and_validate_snapshot")
    assert hasattr(worker, "run_fake_worker")

    # Verify worker module itself doesn't import tracker
    # (Other tests may have imported it, so we check the module's imports)
    import inspect

    worker_source = inspect.getsource(worker)
    assert "from symphony.github" not in worker_source
    assert "import symphony.github" not in worker_source
