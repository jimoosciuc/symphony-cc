"""Tests for the read-only runtime status snapshot API (#55)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from symphony.artifacts import ArtifactWriter
from symphony.config import (
    AgentConfig,
    ClaudeConfig,
    GitHubConfig,
    LoggingConfig,
    PollingConfig,
    RetryConfig,
    SecurityConfig,
    TrackerConfig,
    WorkflowConfig,
    WorkspaceConfig,
)
from symphony.events import AgentEvent
from symphony.github.tracker import FakeGitHubTracker
from symphony.models import Issue, Workspace
from symphony.orchestrator import Orchestrator, WorkerState
from symphony.provider.base import SessionRecord
from symphony.provider.fake import FakeProvider
from symphony.retry import RetryState
from symphony.workspace import WorkspaceManager


def _issue(number: int) -> Issue:
    return Issue(
        id=f"I_{number}",
        number=number,
        identifier=f"acme/proj#{number}",
        owner="acme",
        repo="proj",
        title=f"Issue {number}",
        body="",
        state="open",
        url=f"https://github.com/acme/proj/issues/{number}",
        labels=("symphony-ready",),
    )


def _config(tmp_path: Path) -> WorkflowConfig:
    return WorkflowConfig(
        tracker=TrackerConfig(
            kind="github",
            owner="acme",
            repo="proj",
            token="ghp_secret_token_value_1234567890",
            include_labels=("symphony-ready",),
        ),
        agent=AgentConfig(max_concurrency=1, max_turns=1),
        workspace=WorkspaceConfig(root=tmp_path / "ws"),
        claude=ClaudeConfig(
            model="fake-model",
            permission_mode="acceptEdits",
            session_store=tmp_path / "sessions",
            transcript_store=tmp_path / "transcripts",
            artifact_store=tmp_path / "artifacts",
        ),
        github=GitHubConfig(),
        security=SecurityConfig(profile="restricted"),
        polling=PollingConfig(),
        retry=RetryConfig(initial_backoff_ms=1000, max_backoff_ms=8000),
        logging=LoggingConfig(),
        workflow_path=tmp_path / "WORKFLOW.md",
    )


def _orchestrator(tmp_path: Path, *, issues: list[Issue] | None = None) -> Orchestrator:
    cfg = _config(tmp_path)
    return Orchestrator(
        cfg,
        tracker=FakeGitHubTracker(issues=issues or []),
        provider=FakeProvider(),
        workspace_manager=WorkspaceManager(cfg.workspace),
        clock=lambda: datetime(2026, 5, 8, tzinfo=timezone.utc),
        workflow_reloader=None,
    )


def test_status_snapshot_idle_state_is_redacted(tmp_path: Path) -> None:
    orch = _orchestrator(tmp_path)

    snapshot = orch.status_snapshot()

    assert snapshot["state"] == "idle"
    assert snapshot["run_id"] == orch.run_id
    assert snapshot["security"] == {
        "profile": "restricted",
        "permission_mode": "acceptEdits",
    }
    assert snapshot["active_workers"] == []
    assert "ghp_secret_token_value_1234567890" not in str(snapshot)


def test_status_snapshot_includes_active_worker_and_redacted_event_payload(
    tmp_path: Path,
) -> None:
    orch = _orchestrator(tmp_path)
    issue = _issue(1)
    workspace = Workspace(
        issue_identifier=issue.identifier,
        workspace_key="acme_proj_1",
        path=tmp_path / "ws" / "acme_proj_1",
        repo_path=tmp_path / "ws" / "acme_proj_1",
        created_at=datetime(2026, 5, 8, tzinfo=timezone.utc),
        reused=False,
    )
    artifacts = ArtifactWriter.for_attempt(
        orch.config.claude.artifact_store,
        owner=issue.owner,
        repo=issue.repo,
        issue_number=issue.number,
        attempt=1,
        redact_keys=orch.config.logging.redact_keys,
    )
    worker = WorkerState(
        issue=issue,
        workspace=workspace,
        session=SessionRecord(
            session_id="sym-active",
            provider="fake",
            issue_identifier=issue.identifier,
            issue_number=issue.number,
            workspace_path=workspace.path,
            artifact_dir=artifacts.root,
            started_at=datetime(2026, 5, 8, tzinfo=timezone.utc),
            provider_session_id="provider-1",
        ),
        artifacts=artifacts,
        config=orch.config,
    )
    worker.last_event = AgentEvent(
        event="message_delta",
        timestamp=datetime(2026, 5, 8, tzinfo=timezone.utc),
        session_id="sym-active",
        provider="fake",
        provider_session_id="provider-1",
        issue_identifier=issue.identifier,
        attempt=1,
        payload={"token": "ghp_secret_token_value_1234567890", "text": "ok"},
    )
    worker.recent_events = [
        worker.last_event,
        AgentEvent(
            event="tool_started",
            timestamp=datetime(2026, 5, 8, 0, 0, 1, tzinfo=timezone.utc),
            session_id="sym-active",
            provider="fake",
            provider_session_id="provider-1",
            issue_identifier=issue.identifier,
            attempt=1,
            payload={
                "tool_name": "Bash",
                "input": {"command": "echo ghp_123456789012345678901234"},
            },
        ),
    ]
    orch.active[issue.identifier] = worker

    snapshot = orch.status_snapshot()

    assert snapshot["state"] == "running"
    active = snapshot["active_workers"][0]
    assert active["issue_identifier"] == issue.identifier
    assert active["provider_session_id"] == "provider-1"
    assert active["security_profile"] == "restricted"
    assert active["last_event"]["payload"]["token"] == "<redacted>"
    assert active["last_event"]["payload"]["text"] == "ok"
    assert len(active["recent_events"]) == 2
    assert active["recent_events"][1]["event"] == "tool_started"
    assert "<redacted>" in active["recent_events"][1]["payload"]["input"]["command"]


def test_status_snapshot_includes_retry_queue(tmp_path: Path) -> None:
    orch = _orchestrator(tmp_path)
    retry = RetryState(issue_identifier="acme/proj#1")
    retry.record_failure(
        "temporary provider failure",
        now=datetime(2026, 5, 8, tzinfo=timezone.utc),
        backoff_ms=60_000,
    )
    orch.retry_states[retry.issue_identifier] = retry

    snapshot = orch.status_snapshot()

    assert snapshot["state"] == "retry_waiting"
    item = snapshot["retry_queue"][0]
    assert item["issue_identifier"] == "acme/proj#1"
    assert item["attempts"] == 1
    assert item["next_attempt_at"] == (
        datetime(2026, 5, 8, tzinfo=timezone.utc) + timedelta(seconds=60)
    ).isoformat()


async def test_status_snapshot_includes_recent_finished_worker(tmp_path: Path) -> None:
    issue = _issue(1)
    orch = _orchestrator(tmp_path, issues=[issue])

    result = await orch.run_once()
    snapshot = orch.status_snapshot()

    assert result.finished == [issue.identifier]
    assert snapshot["recent_finished"][0]["issue_identifier"] == issue.identifier
    assert snapshot["recent_finished"][0]["terminal_state"] == "completed"
    assert snapshot["recent_finished"][0]["security_profile"] == "restricted"
    assert snapshot["recent_finished"][0]["permission_denials_count"] == 0
    assert "task_evidence" in snapshot["recent_finished"][0]
    assert "outcome_decided_by" in snapshot["recent_finished"][0]
    assert "no_pr_reason" in snapshot["recent_finished"][0]
