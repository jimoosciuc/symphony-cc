"""Opt-in live E2E harness: GitHub issue → Claude Code → PR.

Skipped by default. Enabled when ALL of:

- ``SYMPHONY_RUN_FULL_E2E=1``
- ``GITHUB_TOKEN`` is non-empty
- The ``claude`` CLI is on ``PATH`` and authenticated
- ``claude-agent-sdk`` is importable

This harness exercises the complete production path:
1. GitHub issue discovery (or use a configured test issue)
2. Claim the issue
3. Workspace populate with git
4. Claude Code session with claude-opus-4-7 by default
5. Branch/commit/PR creation
6. Evidence detector validation
7. Terminal artifacts recording

Requirements from issue #165:
- Must produce a branch and PR or fail with clear task_outcome
- Records artifact paths, issue URL, branch, PR URL, provider_session_id
- Never prints live credentials
- Default CI remains offline and green
"""

from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from symphony.artifacts import ArtifactWriter
from symphony.config import (
    ClaudeConfig,
    GitHubConfig,
    GitHubProjectConfig,
    TrackerConfig,
    WorkspaceConfig,
)
from symphony.events import AgentEvent
from symphony.evidence import DetectorResult, EvidenceDetector
from symphony.github import GitHubTracker
from symphony.models import Issue
from symphony.provider import ClaudeCodeProvider
from symphony.provider.base import SessionRecord, Terminal
from symphony.workspace import GitWorkspacePopulator, WorkspaceManager

_GATE_ENV = "SYMPHONY_RUN_FULL_E2E"
_DEFAULT_MODEL = "claude-opus-4-7"
_PERMISSION_MODE_ENV = "SYMPHONY_E2E_PERMISSION_MODE"
_REQUIRE_PR_ENV = "SYMPHONY_E2E_REQUIRE_PR"
_REDACT_KEYS = ("token", "api_key", "secret", "password", "authorization")


def _gate() -> None:
    """Gate all tests in this module behind explicit opt-in."""
    if os.environ.get(_GATE_ENV) != "1":
        pytest.skip(f"{_GATE_ENV} not set; full E2E tests skipped")
    if not os.environ.get("GITHUB_TOKEN"):
        pytest.skip("GITHUB_TOKEN not set; full E2E tests skipped")
    if shutil.which("claude") is None:
        pytest.skip("`claude` CLI not on PATH; full E2E tests skipped")
    try:
        import claude_agent_sdk  # noqa: F401
    except ImportError:
        pytest.skip("claude-agent-sdk not installed; full E2E tests skipped")


def _get_test_issue_number() -> int | None:
    """Return configured test issue number or None to discover."""
    raw = os.environ.get("SYMPHONY_E2E_TEST_ISSUE")
    if raw:
        try:
            return int(raw)
        except ValueError:
            pytest.fail(f"SYMPHONY_E2E_TEST_ISSUE={raw!r} is not a valid integer")
    return None


def _build_tracker_config() -> tuple[TrackerConfig, GitHubConfig]:
    """Build tracker and GitHub configs from environment."""
    owner = os.environ.get("SYMPHONY_GITHUB_TEST_OWNER", "jimoosciuc")
    repo = os.environ.get("SYMPHONY_GITHUB_TEST_REPO", "symphony-cc")
    tracker = TrackerConfig(
        kind="github",
        owner=owner,
        repo=repo,
        token=os.environ["GITHUB_TOKEN"],
        include_labels=("symphony-ready",),
    )
    github = GitHubConfig(project=GitHubProjectConfig())
    return tracker, github


def _build_claude_config(tmp_path: Path) -> ClaudeConfig:
    """Build Claude config with test-friendly timeouts."""
    return ClaudeConfig(
        model=os.environ.get("SYMPHONY_CLAUDE_TEST_MODEL", _DEFAULT_MODEL),
        permission_mode=os.environ.get(_PERMISSION_MODE_ENV, "acceptEdits"),
        session_store=tmp_path / "sessions",
        transcript_store=tmp_path / "transcripts",
        artifact_store=tmp_path / "artifacts",
        turn_timeout_ms=300_000,  # 5 minutes for real work
        stall_timeout_ms=120_000,  # 2 minutes stall
    )


def _require_completed_with_pr() -> bool:
    """Return whether production-readiness mode requires GitHub PR evidence."""
    return os.environ.get(_REQUIRE_PR_ENV) == "1"


def _build_workspace_config(tmp_path: Path) -> WorkspaceConfig:
    """Build workspace config with git population."""
    return WorkspaceConfig(
        root=tmp_path / "workspaces",
        populate="git",
        after_create=None,
        before_run=None,
        after_run=None,
        before_delete=None,
    )


def _terminal_state_for_event(event_name: str | None) -> Terminal | None:
    if event_name == "turn_completed":
        return Terminal.COMPLETED
    if event_name == "turn_failed":
        return Terminal.FAILED
    if event_name == "turn_cancelled":
        return Terminal.CANCELLED
    return None


def _permission_denials_count(events: list[AgentEvent]) -> int:
    count = 0
    for event in events:
        if event.event != "permission_resolved":
            continue
        payload = event.payload or {}
        if payload.get("allowed") is False or payload.get("status") in {
            "denied",
            "rejected",
        }:
            count += 1
    return count


def _recent_assistant_text(events: list[AgentEvent]) -> str:
    chunks: list[str] = []
    for event in events:
        payload = event.payload or {}
        for key in ("text", "result"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                chunks.append(value)
    return "\n".join(chunks)


def _summarize_detector_result(
    result: DetectorResult,
    *,
    permission_denials_count: int,
) -> dict[str, Any]:
    pr_number = None
    pr_url = None
    branch_name = None
    for entry in result.task_evidence:
        if entry.get("type") == "pr_linked" and pr_number is None:
            pr_number = entry.get("number")
            pr_url = entry.get("url")
        if entry.get("type") == "branch_pushed" and branch_name is None:
            branch_name = entry.get("name")
    return {
        "task_outcome": result.task_outcome,
        "outcome_decided_by": result.outcome_decided_by,
        "permission_denials_count": permission_denials_count,
        "branch_name": branch_name,
        "pr_number": pr_number,
        "pr_url": pr_url,
        "task_evidence": result.task_evidence,
        "no_pr_reason": result.no_pr_reason,
    }


def _write_terminal_json(
    artifact_writer: ArtifactWriter,
    *,
    session: SessionRecord,
    issue: Issue,
    summary: dict[str, Any],
) -> Path:
    terminal_data = {
        "session_id": session.session_id,
        "issue_number": issue.number,
        "provider_session_id": session.provider_session_id,
        **summary,
    }
    return artifact_writer.write_json("terminal.json", terminal_data)


def test_harness_helpers_match_runtime_contracts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Offline guard for the live harness' production API assumptions.

    The live test below is skipped by default, so this test must exercise
    enough of the helper path to catch dataclass/API drift in normal CI.
    """
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test_redacted")
    monkeypatch.delenv("SYMPHONY_CLAUDE_TEST_MODEL", raising=False)
    tracker_cfg, github_cfg = _build_tracker_config()
    claude_cfg = _build_claude_config(tmp_path)
    workspace_cfg = _build_workspace_config(tmp_path)

    assert tracker_cfg.include_labels == ("symphony-ready",)
    assert claude_cfg.model == _DEFAULT_MODEL
    assert claude_cfg.permission_mode == "acceptEdits"
    assert workspace_cfg.root == tmp_path / "workspaces"

    issue = Issue(
        id="I_123",
        number=123,
        identifier="jimoosciuc/symphony-cc#123",
        owner="jimoosciuc",
        repo="symphony-cc",
        title="E2E fixture",
        body="Body",
        state="open",
        url="https://github.com/jimoosciuc/symphony-cc/issues/123",
    )
    session = SessionRecord(
        session_id="sym-test",
        provider="claude_code",
        issue_identifier=issue.identifier,
        issue_number=issue.number,
        workspace_path=tmp_path / "workspace",
        artifact_dir=tmp_path / "artifacts",
        started_at=datetime.now(timezone.utc),
        provider_session_id="provider-test",
    )
    artifact_writer = ArtifactWriter.for_attempt(
        claude_cfg.artifact_store,
        owner=issue.owner,
        repo=issue.repo,
        issue_number=issue.number,
        attempt=session.attempt,
        redact_keys=_REDACT_KEYS,
    )
    detector = EvidenceDetector(github_cfg, client=None)
    detector_result = detector.detect(
        issue=issue,
        terminal_state=Terminal.COMPLETED,
        retryable=False,
        blocked=False,
        permission_denials_count=0,
        last_event=None,
        recent_assistant_text="",
        workspace_path=tmp_path,
    )
    assert detector_result.task_outcome == "unknown"

    completed_with_pr = DetectorResult(
        task_outcome="completed_with_pr",
        task_evidence=[
            {
                "type": "pr_linked",
                "number": 170,
                "url": "https://github.com/jimoosciuc/symphony-cc/pull/170",
            },
            {"type": "branch_pushed", "name": "symphony/test", "head_sha": "abc123"},
        ],
    )
    summary = _summarize_detector_result(
        completed_with_pr,
        permission_denials_count=0,
    )
    assert summary["pr_number"] == 170
    assert summary["pr_url"] == "https://github.com/jimoosciuc/symphony-cc/pull/170"
    assert summary["branch_name"] == "symphony/test"
    terminal_path = _write_terminal_json(
        artifact_writer,
        session=session,
        issue=issue,
        summary=summary,
    )
    assert terminal_path.exists()
    terminal_data = json.loads(terminal_path.read_text())
    assert terminal_data["provider_session_id"] == "provider-test"
    assert terminal_data["task_outcome"] == "completed_with_pr"


def test_harness_allows_pr_capable_permission_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test_redacted")
    monkeypatch.setenv(_PERMISSION_MODE_ENV, "bypassPermissions")
    monkeypatch.setenv(_REQUIRE_PR_ENV, "1")

    claude_cfg = _build_claude_config(tmp_path)

    assert claude_cfg.permission_mode == "bypassPermissions"
    assert _require_completed_with_pr() is True


@pytest.fixture
def evidence_dir(tmp_path: Path) -> Path:
    """Directory for recording E2E evidence."""
    d = tmp_path / "evidence"
    d.mkdir()
    return d


async def test_live_e2e_full_github_to_pr(
    tmp_path: Path, evidence_dir: Path
) -> None:
    """Full E2E: discover or use test issue, claim, run Claude, produce PR.

    This test exercises the complete production path and records evidence
    to satisfy acceptance criteria from issue #165.
    """
    _gate()

    # Setup
    tracker_cfg, github_cfg = _build_tracker_config()
    tracker = GitHubTracker(tracker_cfg, github_cfg)
    claude_cfg = _build_claude_config(tmp_path)
    workspace_cfg = _build_workspace_config(tmp_path)

    # Discover or fetch configured test issue
    test_issue_num = _get_test_issue_number()
    if test_issue_num:
        issues = tracker.fetch_issues_by_numbers([test_issue_num])
        if not issues:
            pytest.fail(
                f"Configured test issue #{test_issue_num} not found in "
                f"{tracker_cfg.owner}/{tracker_cfg.repo}"
            )
        issue = issues[0]
    else:
        candidates = tracker.fetch_candidate_issues()
        if not candidates:
            pytest.skip(
                f"No symphony-ready issues in {tracker_cfg.owner}/{tracker_cfg.repo}"
            )
        issue = candidates[0]

    # Record issue details
    evidence = {
        "issue_number": issue.number,
        "issue_url": issue.url,
        "issue_title": issue.title,
        "owner": issue.owner,
        "repo": issue.repo,
    }

    # Claim issue
    claim_result = tracker.claim_issue(
        issue,
        {"agent_id": "live-e2e-test", "source": "full-live-e2e"},
    )
    if not claim_result.ok:
        pytest.fail(f"Failed to claim issue #{issue.number}: {claim_result.reason}")

    evidence["claimed"] = True

    try:
        # Prepare workspace with git population
        populator = GitWorkspacePopulator(tracker_cfg, github_cfg)
        workspace_mgr = WorkspaceManager(workspace_cfg, populator=populator)
        workspace = workspace_mgr.prepare(issue)

        evidence["workspace_path"] = str(workspace.path)

        # Verify git population
        git_dir = workspace.path / ".git"
        if not git_dir.exists():
            pytest.fail(f"Git population failed: {git_dir} does not exist")

        evidence["git_populated"] = True

        # Start Claude Code session
        provider = ClaudeCodeProvider()
        session = await provider.start_session(issue, workspace.path, claude_cfg)

        evidence["provider_session_id"] = session.provider_session_id
        evidence["permission_mode"] = claude_cfg.permission_mode
        evidence["require_completed_with_pr"] = _require_completed_with_pr()

        # Send initial prompt
        prompt = f"Work on issue #{issue.number}: {issue.title}\n\n{issue.body}"
        terminal_event = None
        events: list[AgentEvent] = []
        turn_count = 0

        async for event in provider.send_input(session, prompt):
            events.append(event)
            turn_count += 1
            if event.event in {"turn_completed", "turn_failed", "turn_cancelled"}:
                terminal_event = event.event
                break

        evidence["turn_count"] = turn_count
        evidence["terminal_event"] = terminal_event

        # Close session
        await provider.close(session)

        # Run evidence detector
        permission_denials_count = _permission_denials_count(events)
        last_event = events[-1] if events else None
        artifact_writer = ArtifactWriter.for_attempt(
            claude_cfg.artifact_store,
            owner=issue.owner,
            repo=issue.repo,
            issue_number=issue.number,
            attempt=session.attempt,
            redact_keys=_REDACT_KEYS,
        )
        detector = EvidenceDetector(github_cfg, client=tracker.client)
        detector_result = detector.detect(
            issue=issue,
            terminal_state=_terminal_state_for_event(terminal_event),
            retryable=terminal_event != "turn_completed",
            blocked=False,
            permission_denials_count=permission_denials_count,
            last_event=last_event,
            recent_assistant_text=_recent_assistant_text(events),
            workspace_path=workspace.path,
        )
        summary = _summarize_detector_result(
            detector_result,
            permission_denials_count=permission_denials_count,
        )
        evidence.update(summary)

        # Write terminal.json
        terminal_path = _write_terminal_json(
            artifact_writer,
            session=session,
            issue=issue,
            summary=summary,
        )

        evidence["terminal_json_path"] = str(terminal_path)

        # Write evidence summary
        evidence_file = evidence_dir / f"e2e_evidence_issue_{issue.number}.json"
        evidence_file.write_text(json.dumps(evidence, indent=2))

        # Assertions per acceptance criteria
        assert detector_result.task_outcome in {
            "completed_with_pr",
            "completed_no_pr_declared",
            "incomplete_no_evidence",
            "incomplete_permission_denied",
        }, f"Unexpected task_outcome: {detector_result.task_outcome}"

        assert detector_result.outcome_decided_by in {
            "detector",
            "derivation",
            "unknown",
        }, f"Unexpected outcome_decided_by: {detector_result.outcome_decided_by}"

        # For successful runs, expect PR creation
        if detector_result.task_outcome == "completed_with_pr":
            assert summary["pr_number"] is not None
            assert summary["pr_url"] is not None
            assert summary["branch_name"] is not None
            assert summary["permission_denials_count"] == 0
        elif _require_completed_with_pr():
            pytest.fail(
                "Full live E2E was run with "
                f"{_REQUIRE_PR_ENV}=1 but task_outcome="
                f"{detector_result.task_outcome!r}; evidence={evidence_file}"
            )

        # Print evidence location (not credentials)
        print(f"\nE2E Evidence recorded: {evidence_file}")
        print(f"Issue: {issue.url}")
        if summary["pr_url"]:
            print(f"PR: {summary['pr_url']}")

    finally:
        # Release issue
        tracker.release_issue(issue, "live-e2e-test-finished")
        evidence["released"] = True
