"""Tests for SSH artifact collection."""

import subprocess
from pathlib import Path

from symphony.remote.scp import SSHArtifactCollector


class FakeScpRunner:
    """Fake scp runner for deterministic testing."""

    def __init__(
        self,
        *,
        files: dict[str, str] | None = None,
        missing_files: set[str] | None = None,
        error_files: dict[str, str] | None = None,
        raise_timeout: bool = False,
    ):
        """Initialize fake scp runner.

        Args:
            files: Dict of filename -> content for successful copies
            missing_files: Set of filenames that should return "No such file"
            error_files: Dict of filename -> error message for failed copies
            raise_timeout: Whether to raise TimeoutExpired
        """
        self.files = files or {}
        self.missing_files = missing_files or set()
        self.error_files = error_files or {}
        self.raise_timeout = raise_timeout
        self.last_args: list[str] | None = None

    def run(
        self,
        args: list[str],
        *,
        capture_output: bool = True,
        text: bool = True,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess:
        """Fake scp run that writes files or returns errors."""
        self.last_args = args

        if self.raise_timeout:
            raise subprocess.TimeoutExpired(args, timeout)

        # Extract remote path and local target from scp args
        # Format: scp -q host:remote_path local_target
        if len(args) < 4 or args[0] != "scp":
            return subprocess.CompletedProcess(
                args=args, returncode=1, stdout="", stderr="Invalid scp command"
            )

        remote_path = args[2]  # host:path
        local_target = Path(args[3])
        filename = remote_path.split("/")[-1]

        # Check if file is in missing set
        if filename in self.missing_files:
            return subprocess.CompletedProcess(
                args=args,
                returncode=1,
                stdout="",
                stderr=f"scp: {remote_path}: No such file or directory",
            )

        # Check if file has error
        if filename in self.error_files:
            return subprocess.CompletedProcess(
                args=args,
                returncode=1,
                stdout="",
                stderr=self.error_files[filename],
            )

        # Write file content if available
        if filename in self.files:
            local_target.parent.mkdir(parents=True, exist_ok=True)
            local_target.write_text(self.files[filename], encoding="utf-8")
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

        # Default: file not found
        return subprocess.CompletedProcess(
            args=args,
            returncode=1,
            stdout="",
            stderr=f"scp: {remote_path}: No such file or directory",
        )


def test_ssh_artifact_collector_copies_all_required_files(tmp_path: Path):
    """Test SSH artifact collector copies all required files."""
    artifact_store = tmp_path / "artifacts"
    artifact_store.mkdir()

    files = {
        "events.jsonl": '{"event": "test"}\n',
        "request.json": '{"prompt": "test"}',
        "session.json": '{"session_id": "test"}',
        "terminal.json": '{"terminal": true}',
    }

    runner = FakeScpRunner(files=files)
    collector = SSHArtifactCollector(
        artifact_store=artifact_store,
        redact_keys=("token",),
        runner=runner,
        host="user@remote-host",
    )

    result = collector.collect(
        "/remote/artifacts/owner_repo_1/1",
        owner="owner",
        repo="repo",
        issue_number=1,
        attempt=1,
    )

    assert len(result.copied) == 4
    assert "events.jsonl" in result.copied
    assert "request.json" in result.copied
    assert "session.json" in result.copied
    assert "terminal.json" in result.copied
    assert not result.missing
    assert not result.errors
    assert not result.partial


def test_ssh_artifact_collector_applies_coordinator_redaction(tmp_path: Path):
    """Test SSH artifact collector applies coordinator-side redaction."""
    artifact_store = tmp_path / "artifacts"
    artifact_store.mkdir()

    # Files contain token that should be redacted
    # Note: .jsonl files are treated as text, so only token-shaped values are redacted
    # .json files have key-based redaction
    files = {
        "events.jsonl": '{"event": "test", "data": "ghp_tokenshapedvalue123456"}\n',
        "request.json": '{"prompt": "test", "token": "ghp_secret123"}',
        "session.json": '{"session_id": "test"}',
        "terminal.json": '{"terminal": true}',
    }

    runner = FakeScpRunner(files=files)
    collector = SSHArtifactCollector(
        artifact_store=artifact_store,
        redact_keys=("token",),
        runner=runner,
        host="user@remote-host",
    )

    result = collector.collect(
        "/remote/artifacts/owner_repo_1/1",
        owner="owner",
        repo="repo",
        issue_number=1,
        attempt=1,
    )

    assert len(result.copied) == 4

    # Check that token-shaped value was redacted in JSONL (text redaction)
    events_content = (result.local_root / "events.jsonl").read_text()
    assert "ghp_tokenshapedvalue123456" not in events_content

    # Check that token key was redacted in JSON (key-based redaction)
    request_content = (result.local_root / "request.json").read_text()
    assert "ghp_secret123" not in request_content
    assert "<redacted>" in request_content


def test_ssh_artifact_collector_handles_missing_files(tmp_path: Path):
    """Test SSH artifact collector classifies missing files."""
    artifact_store = tmp_path / "artifacts"
    artifact_store.mkdir()

    files = {
        "events.jsonl": '{"event": "test"}\n',
        "request.json": '{"prompt": "test"}',
    }
    missing_files = {"session.json", "terminal.json"}

    runner = FakeScpRunner(files=files, missing_files=missing_files)
    collector = SSHArtifactCollector(
        artifact_store=artifact_store,
        redact_keys=("token",),
        runner=runner,
        host="user@remote-host",
    )

    result = collector.collect(
        "/remote/artifacts/owner_repo_1/1",
        owner="owner",
        repo="repo",
        issue_number=1,
        attempt=1,
    )

    assert len(result.copied) == 2
    assert "events.jsonl" in result.copied
    assert "request.json" in result.copied
    assert len(result.missing) == 2
    assert "session.json" in result.missing
    assert "terminal.json" in result.missing
    assert result.partial


def test_ssh_artifact_collector_handles_copy_errors(tmp_path: Path):
    """Test SSH artifact collector captures copy errors."""
    artifact_store = tmp_path / "artifacts"
    artifact_store.mkdir()

    files = {
        "events.jsonl": '{"event": "test"}\n',
    }
    error_files = {
        "request.json": "scp: permission denied",
        "session.json": "scp: connection refused",
    }

    runner = FakeScpRunner(files=files, error_files=error_files, missing_files={"terminal.json"})
    collector = SSHArtifactCollector(
        artifact_store=artifact_store,
        redact_keys=("token",),
        runner=runner,
        host="user@remote-host",
    )

    result = collector.collect(
        "/remote/artifacts/owner_repo_1/1",
        owner="owner",
        repo="repo",
        issue_number=1,
        attempt=1,
    )

    assert len(result.copied) == 1
    assert "events.jsonl" in result.copied
    assert len(result.errors) == 2
    assert any("request.json" in err for err in result.errors)
    assert any("session.json" in err for err in result.errors)
    assert len(result.missing) == 1
    assert "terminal.json" in result.missing
    assert result.partial


def test_ssh_artifact_collector_redacts_token_shaped_stderr(tmp_path: Path):
    """Test SSH artifact collector redacts token-shaped scp stderr."""
    artifact_store = tmp_path / "artifacts"
    artifact_store.mkdir()

    token_shaped = "ghp_abcdefghijklmnopqrstuvwxyz123456"
    runner = FakeScpRunner(
        error_files={
            "events.jsonl": f"scp: auth failed for {token_shaped}",
        },
        missing_files={"request.json", "session.json", "terminal.json"},
    )
    collector = SSHArtifactCollector(
        artifact_store=artifact_store,
        redact_keys=("token",),
        runner=runner,
        host="user@remote-host",
    )

    result = collector.collect(
        "/remote/artifacts/owner_repo_1/1",
        owner="owner",
        repo="repo",
        issue_number=1,
        attempt=1,
    )

    errors = "\n".join(result.errors)
    assert token_shaped not in errors
    assert "<redacted>" in errors


def test_ssh_artifact_collector_handles_timeout(tmp_path: Path):
    """Test SSH artifact collector handles scp timeout."""
    artifact_store = tmp_path / "artifacts"
    artifact_store.mkdir()

    runner = FakeScpRunner(raise_timeout=True)
    collector = SSHArtifactCollector(
        artifact_store=artifact_store,
        redact_keys=("token",),
        runner=runner,
        host="user@remote-host",
        timeout_seconds=60.0,
    )

    result = collector.collect(
        "/remote/artifacts/owner_repo_1/1",
        owner="owner",
        repo="repo",
        issue_number=1,
        attempt=1,
    )

    assert not result.copied
    assert len(result.errors) == 4  # All files timeout
    assert all("timed out" in err for err in result.errors)
    assert result.partial


def test_ssh_artifact_collector_redacts_token_shaped_values(tmp_path: Path):
    """Test SSH artifact collector redacts token-shaped values."""
    artifact_store = tmp_path / "artifacts"
    artifact_store.mkdir()

    # Files contain token-shaped values
    token_shaped = "ghp_abcdefghijklmnopqrstuvwxyz123456"
    files = {
        "events.jsonl": f'{{"event": "test", "data": "{token_shaped}"}}\n',
        "request.json": f'{{"prompt": "test {token_shaped}"}}',
        "session.json": '{"session_id": "test"}',
        "terminal.json": '{"terminal": true}',
    }

    runner = FakeScpRunner(files=files)
    collector = SSHArtifactCollector(
        artifact_store=artifact_store,
        redact_keys=("token",),
        runner=runner,
        host="user@remote-host",
    )

    result = collector.collect(
        "/remote/artifacts/owner_repo_1/1",
        owner="owner",
        repo="repo",
        issue_number=1,
        attempt=1,
    )

    assert len(result.copied) == 4

    # Check that token-shaped value was redacted
    events_content = (result.local_root / "events.jsonl").read_text()
    assert token_shaped not in events_content

    request_content = (result.local_root / "request.json").read_text()
    assert token_shaped not in request_content


def test_ssh_artifact_collector_builds_correct_scp_command(tmp_path: Path):
    """Test SSH artifact collector builds correct scp command."""
    artifact_store = tmp_path / "artifacts"
    artifact_store.mkdir()

    files = {"events.jsonl": '{"event": "test"}\n'}

    runner = FakeScpRunner(files=files)
    collector = SSHArtifactCollector(
        artifact_store=artifact_store,
        redact_keys=("token",),
        runner=runner,
        host="user@remote-host",
    )

    collector.collect(
        "/remote/artifacts/owner_repo_1/1",
        owner="owner",
        repo="repo",
        issue_number=1,
        attempt=1,
    )

    # Check that scp command was built correctly
    # Note: last_args will be from the last file (terminal.json), but format is consistent
    assert runner.last_args is not None
    assert runner.last_args[0] == "scp"
    assert "-q" in runner.last_args
    # Check that remote path includes host and path
    remote_arg = runner.last_args[2]
    assert remote_arg.startswith("user@remote-host:/remote/artifacts/owner_repo_1/1/")


def test_ssh_artifact_collector_no_tracker_token_in_command(tmp_path: Path):
    """Test SSH artifact collector doesn't include tracker token in scp command."""
    artifact_store = tmp_path / "artifacts"
    artifact_store.mkdir()

    files = {"events.jsonl": '{"event": "test"}\n'}

    runner = FakeScpRunner(files=files)
    collector = SSHArtifactCollector(
        artifact_store=artifact_store,
        redact_keys=("token",),
        runner=runner,
        host="user@remote-host",
    )

    collector.collect(
        "/remote/artifacts/owner_repo_1/1",
        owner="owner",
        repo="repo",
        issue_number=1,
        attempt=1,
    )

    # Check that no token-like values appear in command
    command_str = " ".join(runner.last_args or [])
    assert "ghp_" not in command_str
    assert "token" not in command_str.lower()
