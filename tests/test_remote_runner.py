"""Tests for pre-orchestrator remote dispatch runner composition."""

from __future__ import annotations

from pathlib import Path

from symphony.config import build_config
from symphony.models import Issue
from symphony.remote.artifacts import ArtifactCollectionResult
from symphony.remote.plan import RemoteDispatchPlan, build_remote_dispatch_plan
from symphony.remote.runner import RemoteDispatchRunner, RemoteDispatchRunResult
from symphony.remote.transport import RemoteRunResult
from symphony.remote.upload import PayloadUploadResult


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


class FakeUploader:
    def __init__(self, result: PayloadUploadResult, order: list[str]) -> None:
        self.result = result
        self.order = order
        self.plans: list[RemoteDispatchPlan] = []

    def upload(self, plan: RemoteDispatchPlan) -> PayloadUploadResult:
        self.order.append("upload")
        self.plans.append(plan)
        return self.result


class FakeTransport:
    def __init__(self, result: RemoteRunResult, order: list[str]) -> None:
        self.result = result
        self.order = order
        self.run_count = 0

    def run(self, config) -> RemoteRunResult:
        self.order.append("transport")
        self.run_count += 1
        return self.result


class FakeTransportFactory:
    def __init__(self, transport: FakeTransport, order: list[str]) -> None:
        self.transport = transport
        self.order = order
        self.calls: list[dict[str, str]] = []

    def __call__(
        self,
        *,
        remote_snapshot_path: str,
        remote_dispatch_path: str,
    ) -> FakeTransport:
        self.order.append("transport_factory")
        self.calls.append(
            {
                "remote_snapshot_path": remote_snapshot_path,
                "remote_dispatch_path": remote_dispatch_path,
            }
        )
        return self.transport


class FakeArtifactCollector:
    def __init__(
        self,
        result: ArtifactCollectionResult | Exception,
        order: list[str],
    ) -> None:
        self.result = result
        self.order = order
        self.calls: list[dict[str, object]] = []

    def collect(
        self,
        remote_root: str,
        *,
        owner: str,
        repo: str,
        issue_number: int,
        attempt: int,
    ) -> ArtifactCollectionResult:
        self.order.append("artifacts")
        self.calls.append(
            {
                "remote_root": remote_root,
                "owner": owner,
                "repo": repo,
                "issue_number": issue_number,
                "attempt": attempt,
            }
        )
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def test_remote_dispatch_runner_success_calls_steps_in_order(tmp_path: Path):
    """Test runner materializes, uploads, and runs transport in order."""
    config = _config(tmp_path)
    plan = build_remote_dispatch_plan(_issue(), attempt=1, config=config)
    order: list[str] = []
    uploader = FakeUploader(
        PayloadUploadResult(
            uploaded=(plan.remote_snapshot_path, plan.remote_dispatch_path),
            errors=(),
        ),
        order,
    )
    transport = FakeTransport(RemoteRunResult(), order)
    transport_factory = FakeTransportFactory(transport, order)
    artifact_result = ArtifactCollectionResult(
        local_root=tmp_path / "artifacts" / "test-owner_test-repo_42" / "1",
        copied=("events.jsonl",),
    )
    artifact_collector = FakeArtifactCollector(artifact_result, order)
    runner = RemoteDispatchRunner(
        uploader=uploader,
        transport_factory=transport_factory,
        artifact_collector=artifact_collector,
    )

    result = runner.run(plan, config)

    assert isinstance(result, RemoteDispatchRunResult)
    assert result.ok
    assert result.materialized is not None
    assert result.materialized.snapshot_path.exists()
    assert result.materialized.dispatch_path.exists()
    assert result.upload is uploader.result
    assert result.transport is transport.result
    assert result.artifacts is artifact_result
    assert order == ["upload", "transport_factory", "transport", "artifacts"]


def test_remote_dispatch_runner_passes_artifact_inputs_from_plan(tmp_path: Path):
    """Test artifact collector receives remote artifact path and issue identity."""
    config = _config(tmp_path)
    plan = build_remote_dispatch_plan(_issue(), attempt=2, config=config)
    order: list[str] = []
    uploader = FakeUploader(
        PayloadUploadResult(uploaded=(plan.remote_snapshot_path, plan.remote_dispatch_path)),
        order,
    )
    transport = FakeTransport(RemoteRunResult(), order)
    transport_factory = FakeTransportFactory(transport, order)
    artifact_result = ArtifactCollectionResult(local_root=tmp_path / "artifacts")
    artifact_collector = FakeArtifactCollector(artifact_result, order)
    runner = RemoteDispatchRunner(
        uploader=uploader,
        transport_factory=transport_factory,
        artifact_collector=artifact_collector,
    )

    runner.run(plan, config)

    assert artifact_collector.calls == [
        {
            "remote_root": plan.remote_artifact_path,
            "owner": "test-owner",
            "repo": "test-repo",
            "issue_number": 42,
            "attempt": 2,
        }
    ]


def test_remote_dispatch_runner_passes_staged_paths_to_transport(tmp_path: Path):
    """Test transport factory receives plan remote snapshot/dispatch paths."""
    config = _config(tmp_path)
    plan = build_remote_dispatch_plan(_issue(), attempt=1, config=config)
    order: list[str] = []
    uploader = FakeUploader(
        PayloadUploadResult(uploaded=(plan.remote_snapshot_path, plan.remote_dispatch_path)),
        order,
    )
    transport = FakeTransport(RemoteRunResult(), order)
    transport_factory = FakeTransportFactory(transport, order)
    runner = RemoteDispatchRunner(uploader=uploader, transport_factory=transport_factory)

    runner.run(plan, config)

    assert transport_factory.calls == [
        {
            "remote_snapshot_path": plan.remote_snapshot_path,
            "remote_dispatch_path": plan.remote_dispatch_path,
        }
    ]


def test_remote_dispatch_runner_upload_failure_prevents_transport(tmp_path: Path):
    """Test upload failure stops before transport and artifact collection."""
    config = _config(tmp_path)
    plan = build_remote_dispatch_plan(_issue(), attempt=1, config=config)
    order: list[str] = []
    uploader = FakeUploader(PayloadUploadResult(errors=("network failed",)), order)
    transport = FakeTransport(RemoteRunResult(), order)
    transport_factory = FakeTransportFactory(transport, order)
    artifact_collector = FakeArtifactCollector(
        ArtifactCollectionResult(local_root=tmp_path / "artifacts"),
        order,
    )
    runner = RemoteDispatchRunner(
        uploader=uploader,
        transport_factory=transport_factory,
        artifact_collector=artifact_collector,
    )

    result = runner.run(plan, config)

    assert result.failed
    assert result.transport is None
    assert result.artifacts is None
    assert transport.run_count == 0
    assert transport_factory.calls == []
    assert artifact_collector.calls == []
    assert result.errors == ("upload failed: network failed",)
    assert order == ["upload"]


def test_remote_dispatch_runner_transport_failure_propagates(tmp_path: Path):
    """Test transport failure propagates and still allows artifact collection."""
    config = _config(tmp_path)
    plan = build_remote_dispatch_plan(_issue(), attempt=1, config=config)
    order: list[str] = []
    uploader = FakeUploader(
        PayloadUploadResult(uploaded=(plan.remote_snapshot_path, plan.remote_dispatch_path)),
        order,
    )
    transport_result = RemoteRunResult(errors=("SSH stderr: permission denied",), failed=True)
    transport = FakeTransport(transport_result, order)
    artifact_result = ArtifactCollectionResult(
        local_root=tmp_path / "artifacts",
        copied=("terminal.json",),
    )
    artifact_collector = FakeArtifactCollector(artifact_result, order)
    runner = RemoteDispatchRunner(
        uploader=uploader,
        transport_factory=FakeTransportFactory(transport, order),
        artifact_collector=artifact_collector,
    )

    result = runner.run(plan, config)

    assert result.failed
    assert result.transport is transport_result
    assert result.artifacts is artifact_result
    assert result.errors == transport_result.errors
    assert order == ["upload", "transport_factory", "transport", "artifacts"]


def test_remote_dispatch_runner_artifact_errors_are_reflected(tmp_path: Path):
    """Test artifact collection errors become combined runner errors."""
    config = _config(tmp_path)
    plan = build_remote_dispatch_plan(_issue(), attempt=1, config=config)
    order: list[str] = []
    uploader = FakeUploader(
        PayloadUploadResult(uploaded=(plan.remote_snapshot_path, plan.remote_dispatch_path)),
        order,
    )
    transport = FakeTransport(RemoteRunResult(), order)
    artifact_result = ArtifactCollectionResult(
        local_root=tmp_path / "artifacts",
        errors=("events.jsonl: permission denied",),
    )
    runner = RemoteDispatchRunner(
        uploader=uploader,
        transport_factory=FakeTransportFactory(transport, order),
        artifact_collector=FakeArtifactCollector(artifact_result, order),
    )

    result = runner.run(plan, config)

    assert result.failed
    assert result.artifacts is artifact_result
    assert result.errors == artifact_result.errors


def test_remote_dispatch_runner_artifact_exception_is_reflected_and_redacted(
    tmp_path: Path,
):
    """Test collector exceptions are converted to redacted artifact errors."""
    config = _config(tmp_path)
    plan = build_remote_dispatch_plan(_issue(), attempt=1, config=config)
    order: list[str] = []
    uploader = FakeUploader(
        PayloadUploadResult(uploaded=(plan.remote_snapshot_path, plan.remote_dispatch_path)),
        order,
    )
    transport = FakeTransport(RemoteRunResult(), order)
    artifact_collector = FakeArtifactCollector(
        RuntimeError(f"failed with token {config.tracker.token}"),
        order,
    )
    runner = RemoteDispatchRunner(
        uploader=uploader,
        transport_factory=FakeTransportFactory(transport, order),
        artifact_collector=artifact_collector,
    )

    result = runner.run(plan, config)

    assert result.failed
    assert result.artifacts is not None
    assert result.artifacts.local_root == (
        config.claude.artifact_store / "test-owner_test-repo_42" / "1"
    )
    assert result.errors == result.artifacts.errors
    errors = " ".join(result.errors)
    assert "artifact collection failed:" in errors
    assert config.tracker.token not in errors
    assert "ghp_" not in errors


def test_remote_dispatch_runner_errors_do_not_include_tracker_token(tmp_path: Path):
    """Test runner result errors can preserve upstream redaction boundary."""
    config = _config(tmp_path)
    plan = build_remote_dispatch_plan(_issue(), attempt=1, config=config)
    order: list[str] = []
    uploader = FakeUploader(PayloadUploadResult(errors=("auth <redacted>",)), order)
    transport = FakeTransport(RemoteRunResult(), order)
    runner = RemoteDispatchRunner(
        uploader=uploader,
        transport_factory=FakeTransportFactory(transport, order),
    )

    result = runner.run(plan, config)

    errors = " ".join(result.errors)
    assert config.tracker.token not in errors
    assert "ghp_" not in errors


def test_remote_dispatch_runner_without_collector_skips_artifacts(tmp_path: Path):
    """Test runner works without artifact collector (optional)."""
    config = _config(tmp_path)
    plan = build_remote_dispatch_plan(_issue(), attempt=1, config=config)
    order: list[str] = []
    uploader = FakeUploader(
        PayloadUploadResult(uploaded=(plan.remote_snapshot_path, plan.remote_dispatch_path)),
        order,
    )
    transport = FakeTransport(RemoteRunResult(), order)
    transport_factory = FakeTransportFactory(transport, order)
    runner = RemoteDispatchRunner(
        uploader=uploader,
        transport_factory=transport_factory,
        artifact_collector=None,  # No collector
    )

    result = runner.run(plan, config)

    assert result.ok
    assert result.artifacts is None
    assert order == ["upload", "transport_factory", "transport"]
