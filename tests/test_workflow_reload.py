"""Tests for workflow reload last-known-good behavior (#70)."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from symphony.artifact_retention import REPORT_DIR_NAME
from symphony.artifacts import ArtifactWriter
from symphony.github.tracker import FakeGitHubTracker
from symphony.models import Issue, Workspace
from symphony.orchestrator import Orchestrator, WorkerState
from symphony.provider.base import SessionRecord
from symphony.provider.fake import FakeProvider
from symphony.workflow import load_workflow
from symphony.workflow_reload import WorkflowReloader
from symphony.workspace import WorkspaceManager


def _workflow_text(
    tmp_path: Path,
    *,
    max_concurrency: int = 1,
    exclude_label: str = "symphony-blocked",
    token: str = "$GITHUB_TOKEN",
) -> str:
    return f"""---
tracker:
  kind: github
  owner: acme
  repo: proj
  token: {token}
  include_labels: [symphony-ready]
  exclude_labels: [{exclude_label}]
agent:
  provider: claude_code
  max_concurrency: {max_concurrency}
  max_turns: 1
workspace:
  root: {tmp_path / "ws"}
claude:
  model: fake-model
  permission_mode: acceptEdits
  session_store: {tmp_path / "sessions"}
  transcript_store: {tmp_path / "transcripts"}
  artifact_store: {tmp_path / "artifacts"}
github: {{}}
retry:
  initial_backoff_ms: 1000
  max_backoff_ms: 8000
  multiplier: 2.0
---
Please work on {{{{ issue.identifier }}}}.
"""


def _write_workflow(path: Path, text: str, *, mtime: int) -> None:
    path.write_text(text, encoding="utf-8")
    os.utime(path, ns=(mtime, mtime))


def _issue(number: int, *, labels: tuple[str, ...] = ("symphony-ready",)) -> Issue:
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
        labels=labels,
    )


def _orchestrator(
    workflow_path: Path,
    *,
    env: dict[str, str],
    issues: list[Issue],
) -> tuple[Orchestrator, FakeGitHubTracker]:
    workflow = load_workflow(workflow_path, env=env)
    tracker = FakeGitHubTracker(issues=issues)
    orch = Orchestrator(
        workflow.config,
        tracker=tracker,
        provider=FakeProvider(),
        workspace_manager=WorkspaceManager(workflow.config.workspace),
        workflow_reloader=WorkflowReloader.from_workflow(workflow, env=env),
        clock=lambda: datetime(2026, 5, 8, tzinfo=timezone.utc),
    )
    return orch, tracker


async def test_valid_workflow_reload_applies_to_future_dispatch(tmp_path: Path) -> None:
    workflow_path = tmp_path / "WORKFLOW.md"
    env = {"GITHUB_TOKEN": "literal-token"}
    _write_workflow(
        workflow_path,
        _workflow_text(tmp_path, max_concurrency=1),
        mtime=1_000_000_000,
    )
    orch, _tracker = _orchestrator(
        workflow_path,
        env=env,
        issues=[_issue(1), _issue(2)],
    )
    _write_workflow(
        workflow_path,
        _workflow_text(tmp_path, max_concurrency=2),
        mtime=2_000_000_000,
    )

    result = await orch.run_once()

    assert result.workflow_reloaded is True
    assert result.dispatch_paused is False
    assert len(result.dispatched) == 2
    assert orch.config.agent.max_concurrency == 2


async def test_invalid_workflow_reload_keeps_last_known_good_and_pauses_dispatch(
    tmp_path: Path,
) -> None:
    workflow_path = tmp_path / "WORKFLOW.md"
    env = {"GITHUB_TOKEN": "literal-token"}
    _write_workflow(
        workflow_path,
        _workflow_text(tmp_path, max_concurrency=1),
        mtime=1_000_000_000,
    )
    orch, _tracker = _orchestrator(workflow_path, env=env, issues=[_issue(1)])
    _write_workflow(workflow_path, "---\ntracker: [bad]\n---\n", mtime=2_000_000_000)

    result = await orch.run_once()

    assert result.workflow_reloaded is False
    assert result.dispatch_paused is True
    assert result.dispatched == []
    assert orch.config.agent.max_concurrency == 1

    events = (
        tmp_path
        / "artifacts"
        / REPORT_DIR_NAME
        / "_reload_events.jsonl"
    )
    payload = json.loads(events.read_text(encoding="utf-8").splitlines()[-1])
    assert payload["outcome"] == "rejected_parse"


async def test_restart_required_workflow_change_is_rejected(tmp_path: Path) -> None:
    workflow_path = tmp_path / "WORKFLOW.md"
    env = {"GITHUB_TOKEN": "literal-token"}
    _write_workflow(
        workflow_path,
        _workflow_text(tmp_path, max_concurrency=1),
        mtime=1_000_000_000,
    )
    orch, _tracker = _orchestrator(workflow_path, env=env, issues=[_issue(1)])
    _write_workflow(
        workflow_path,
        _workflow_text(tmp_path, max_concurrency=1, token="different-token"),
        mtime=2_000_000_000,
    )

    result = await orch.run_once()

    assert result.dispatch_paused is True
    assert "tracker.token" in (result.workflow_reload_error or "")
    assert orch.config.tracker.token == "literal-token"


async def test_active_worker_keeps_snapshot_during_reload(tmp_path: Path) -> None:
    workflow_path = tmp_path / "WORKFLOW.md"
    env = {"GITHUB_TOKEN": "literal-token"}
    _write_workflow(
        workflow_path,
        _workflow_text(tmp_path, exclude_label="old-blocked"),
        mtime=1_000_000_000,
    )
    issue = _issue(1, labels=("symphony-ready", "new-blocked"))
    orch, tracker = _orchestrator(workflow_path, env=env, issues=[issue])

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
    orch.active[issue.identifier] = WorkerState(
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
        ),
        artifacts=artifacts,
        config=orch.config,
    )
    tracker.set_issue_labels(issue.identifier, ("symphony-ready", "new-blocked"))
    _write_workflow(
        workflow_path,
        _workflow_text(tmp_path, exclude_label="new-blocked"),
        mtime=2_000_000_000,
    )

    result = await orch.run_once()

    assert result.workflow_reloaded is True
    assert result.reconciled_cancelled == []
    assert issue.identifier in orch.active
