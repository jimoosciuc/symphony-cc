"""Tests for usage accounting from normalized provider events (#54)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from symphony.config import (
    AgentConfig,
    ClaudeConfig,
    GitHubConfig,
    LoggingConfig,
    PollingConfig,
    RetryConfig,
    TrackerConfig,
    WorkflowConfig,
    WorkspaceConfig,
)
from symphony.events import AgentEvent
from symphony.github.tracker import FakeGitHubTracker
from symphony.models import Issue
from symphony.orchestrator import Orchestrator
from symphony.provider.fake import FakeProvider, FakeTurnScript
from symphony.usage import UsageTotals, extract_usage
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
            artifact_store=tmp_path / "artifacts",
        ),
        github=GitHubConfig(),
        polling=PollingConfig(),
        retry=RetryConfig(initial_backoff_ms=1000, max_backoff_ms=8000),
        logging=LoggingConfig(),
        workflow_path=tmp_path / "WORKFLOW.md",
    )


def _orchestrator(
    tmp_path: Path,
    *,
    provider: FakeProvider,
    issues: list[Issue],
) -> Orchestrator:
    cfg = _config(tmp_path)
    return Orchestrator(
        cfg,
        tracker=FakeGitHubTracker(issues=issues),
        provider=provider,
        workspace_manager=WorkspaceManager(cfg.workspace),
        clock=lambda: datetime(2026, 5, 8, tzinfo=timezone.utc),
        workflow_reloader=None,
    )


async def test_usage_events_are_persisted_to_usage_and_terminal_artifacts(
    tmp_path: Path,
) -> None:
    issue = _issue(1)
    provider = FakeProvider(
        FakeTurnScript(
            events=[
                (
                    "usage",
                    {
                        "input_tokens": 10,
                        "output_tokens": 5,
                        "cache_read_input_tokens": 2,
                        "cost_usd": 0.012,
                    },
                ),
                ("turn_completed", {}),
            ]
        )
    )
    orch = _orchestrator(tmp_path, provider=provider, issues=[issue])

    await orch.run_once()

    artifact_dir = tmp_path / "artifacts" / "acme_proj_1" / "1"
    usage = json.loads((artifact_dir / "usage.json").read_text(encoding="utf-8"))
    terminal = json.loads((artifact_dir / "terminal.json").read_text(encoding="utf-8"))
    assert usage["input_tokens"] == 10
    assert usage["output_tokens"] == 5
    assert usage["cache_read_input_tokens"] == 2
    assert usage["total_tokens"] == 17
    assert usage["cost_usd"] == 0.012
    assert terminal["usage"] == usage
    assert orch.status_snapshot()["recent_finished"][0]["usage"] == usage


def test_usage_can_be_extracted_from_nested_heartbeat_payload() -> None:
    event = AgentEvent(
        event="heartbeat",
        timestamp=datetime(2026, 5, 8, tzinfo=timezone.utc),
        session_id="sym-1",
        provider="fake",
        issue_identifier="acme/proj#1",
        attempt=1,
        payload={"usage": {"input_tokens": "3", "output_tokens": 4}},
    )
    totals = UsageTotals()

    assert extract_usage(event) == {"input_tokens": "3", "output_tokens": 4}
    assert totals.apply_event(event) is True
    assert totals.to_json()["total_tokens"] == 7


def test_malformed_usage_payload_is_ignored() -> None:
    event = AgentEvent(
        event="usage",
        timestamp=datetime(2026, 5, 8, tzinfo=timezone.utc),
        session_id="sym-1",
        provider="fake",
        issue_identifier="acme/proj#1",
        attempt=1,
        payload={"usage": "not-a-mapping"},
    )
    totals = UsageTotals()

    assert extract_usage(event) is None
    assert totals.apply_event(event) is False
    assert totals.has_usage is False
