"""Tests for artifact retention cleanup and reporting (#67)."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from symphony.artifact_retention import REPORT_DIR_NAME, ArtifactRetentionExecutor
from symphony.config import (
    AgentConfig,
    ArtifactRetentionConfig,
    ClaudeConfig,
    GitHubConfig,
    LoggingConfig,
    PollingConfig,
    RetryConfig,
    TrackerConfig,
    WorkflowConfig,
    WorkspaceConfig,
)
from symphony.github.tracker import FakeGitHubTracker
from symphony.orchestrator import Orchestrator
from symphony.provider.fake import FakeProvider
from symphony.workspace import WorkspaceManager


def _attempt(root: Path, issue_key: str, attempt: str, *, mtime: datetime) -> Path:
    path = root / issue_key / attempt
    path.mkdir(parents=True)
    (path / "terminal.json").write_text('{"ok": true}', encoding="utf-8")
    ts = mtime.timestamp()
    os.utime(path, (ts, ts))
    return path


def _executor(
    root: Path,
    *,
    enabled: bool = True,
    dry_run: bool = False,
    redact_keys: tuple[str, ...] = ("token", "authorization", "api_key", "password"),
) -> ArtifactRetentionExecutor:
    return ArtifactRetentionExecutor(
        root,
        ArtifactRetentionConfig(enabled=enabled, max_age_days=7, dry_run=dry_run),
        redact_keys=redact_keys,
        clock=lambda: datetime(2026, 5, 8, tzinfo=timezone.utc),
    )


def test_artifact_retention_disabled_is_noop(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    old = _attempt(root, "acme_proj_1", "1", mtime=datetime(2026, 1, 1, tzinfo=timezone.utc))

    report = _executor(root, enabled=False).sweep()

    assert report is None
    assert old.exists()
    assert not (root / REPORT_DIR_NAME).exists()


def test_artifact_retention_deletes_old_attempt_and_keeps_young(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    old = _attempt(root, "acme_proj_1", "1", mtime=datetime(2026, 4, 1, tzinfo=timezone.utc))
    young = _attempt(root, "acme_proj_1", "2", mtime=datetime(2026, 5, 7, tzinfo=timezone.utc))

    report = _executor(root).sweep()

    assert not old.exists()
    assert young.exists()
    assert report is not None
    assert report.considered == 2
    assert report.deleted == 1
    assert report.skipped == 1
    assert report.errors == 0


def test_artifact_retention_dry_run_reports_without_deleting(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    old = _attempt(root, "acme_proj_1", "1", mtime=datetime(2026, 4, 1, tzinfo=timezone.utc))

    report = _executor(root, dry_run=True).sweep()

    assert old.exists()
    assert report is not None
    assert report.deleted == 0
    assert report.skipped == 1
    assert report.decisions[0].action == "skipped_dry_run"


def test_artifact_retention_writes_redacted_report(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    _attempt(root, "acme_proj_1", "1", mtime=datetime(2026, 4, 1, tzinfo=timezone.utc))

    _executor(root, dry_run=True, redact_keys=("reason",)).sweep()

    reports = sorted((root / REPORT_DIR_NAME).glob("artifact-retention-*.json"))
    assert len(reports) == 1
    payload = json.loads(reports[0].read_text(encoding="utf-8"))
    assert payload["summary"] == {
        "considered": 1,
        "deleted": 0,
        "skipped": 1,
        "errors": 0,
    }
    assert payload["decisions"][0]["reason"] == "<redacted>"


def test_artifact_retention_does_not_overwrite_same_second_reports(
    tmp_path: Path,
) -> None:
    root = tmp_path / "artifacts"
    _attempt(root, "acme_proj_1", "1", mtime=datetime(2026, 4, 1, tzinfo=timezone.utc))

    executor = _executor(root, dry_run=True)
    executor.sweep()
    executor.sweep()

    reports = sorted((root / REPORT_DIR_NAME).glob("artifact-retention-*.json"))
    assert {path.name for path in reports} == {
        "artifact-retention-20260508T000000Z.json",
        "artifact-retention-20260508T000000Z-2.json",
    }


def test_artifact_retention_records_delete_errors(
    tmp_path: Path, monkeypatch
) -> None:
    import symphony.artifact_retention as retention

    root = tmp_path / "artifacts"
    old = _attempt(root, "acme_proj_1", "1", mtime=datetime(2026, 4, 1, tzinfo=timezone.utc))

    def _fail(_path: Path) -> None:
        raise OSError("permission denied")

    monkeypatch.setattr(retention.shutil, "rmtree", _fail)

    report = _executor(root).sweep()

    assert old.exists()
    assert report is not None
    assert report.errors == 1
    assert report.decisions[0].action == "error"


async def test_orchestrator_runs_artifact_retention_before_dispatch(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    old = _attempt(root, "acme_proj_1", "1", mtime=datetime(2026, 4, 1, tzinfo=timezone.utc))
    cfg = WorkflowConfig(
        tracker=TrackerConfig(
            kind="github",
            owner="acme",
            repo="proj",
            token="literal-token",
            include_labels=("symphony-ready",),
        ),
        agent=AgentConfig(max_concurrency=1, max_turns=1),
        workspace=WorkspaceConfig(root=tmp_path / "ws"),
        claude=ClaudeConfig(
            model="fake-model",
            permission_mode="acceptEdits",
            session_store=tmp_path / "sessions",
            transcript_store=tmp_path / "transcripts",
            artifact_store=root,
            artifact_retention=ArtifactRetentionConfig(enabled=True, max_age_days=7),
        ),
        github=GitHubConfig(),
        polling=PollingConfig(),
        retry=RetryConfig(),
        logging=LoggingConfig(),
        workflow_path=tmp_path / "WORKFLOW.md",
    )
    orch = Orchestrator(
        cfg,
        tracker=FakeGitHubTracker(issues=[]),
        provider=FakeProvider(),
        workspace_manager=WorkspaceManager(cfg.workspace),
        clock=lambda: datetime(2026, 5, 8, tzinfo=timezone.utc),
    )

    await orch.run_once()

    assert not old.exists()
    assert list((root / REPORT_DIR_NAME).glob("artifact-retention-*.json"))
