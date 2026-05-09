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
4. Claude Code session with [REDACTED]
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
from pathlib import Path

import pytest

from symphony.artifacts import ArtifactWriter
from symphony.config import (
    ClaudeConfig,
    GitHubConfig,
    GitHubProjectConfig,
    TrackerConfig,
    WorkspaceConfig,
)
from symphony.evidence import EvidenceDetector
from symphony.github import GitHubTracker
from symphony.provider import ClaudeCodeProvider
from symphony.workspace import GitWorkspacePopulator, WorkspaceManager

_GATE_ENV = "SYMPHONY_RUN_FULL_E2E"


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
        model=os.environ.get("SYMPHONY_CLAUDE_TEST_MODEL", "[REDACTED]"),
        permission_mode="acceptEdits",
        session_store=tmp_path / "sessions",
        transcript_store=tmp_path / "transcripts",
        artifact_store=tmp_path / "artifacts",
        turn_timeout_ms=300_000,  # 5 minutes for real work
        stall_timeout_ms=120_000,  # 2 minutes stall
    )


def _build_workspace_config(tmp_path: Path) -> WorkspaceConfig:
    """Build workspace config with git population."""
    return WorkspaceConfig(
        path=tmp_path / "workspaces",
        populate="git",
        after_create=None,
        before_run=None,
        after_run=None,
        before_delete=None,
    )


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
    claim_result = tracker.claim_issue(issue, agent_id="live-e2e-test")
    if not claim_result.success:
        pytest.fail(f"Failed to claim issue #{issue.number}: {claim_result.message}")

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

        # Send initial prompt
        prompt = f"Work on issue #{issue.number}: {issue.title}\n\n{issue.body}"
        terminal_event = None
        turn_count = 0

        async for event in provider.send_input(session, prompt):
            turn_count += 1
            if event.event in {"turn_completed", "turn_failed", "turn_cancelled"}:
                terminal_event = event.event
                break

        evidence["turn_count"] = turn_count
        evidence["terminal_event"] = terminal_event

        # Close session
        await provider.close(session)

        # Run evidence detector
        artifact_writer = ArtifactWriter(claude_cfg.artifact_store / session.session_id)
        detector = EvidenceDetector(tracker, workspace, artifact_writer)
        detector_result = detector.detect()

        evidence["task_outcome"] = detector_result.task_outcome
        evidence["outcome_decided_by"] = detector_result.outcome_decided_by
        evidence["permission_denials_count"] = detector_result.permission_denials_count
        evidence["branch_name"] = detector_result.branch_name
        evidence["pr_number"] = detector_result.pr_number
        evidence["pr_url"] = detector_result.pr_url

        # Write terminal.json
        terminal_path = artifact_writer.artifact_dir / "terminal.json"
        terminal_data = {
            "session_id": session.session_id,
            "issue_number": issue.number,
            "task_outcome": detector_result.task_outcome,
            "outcome_decided_by": detector_result.outcome_decided_by,
            "permission_denials_count": detector_result.permission_denials_count,
            "branch_name": detector_result.branch_name,
            "pr_number": detector_result.pr_number,
            "pr_url": detector_result.pr_url,
        }
        terminal_path.write_text(json.dumps(terminal_data, indent=2))

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
            "timeout",
            "error",
        }, f"Unexpected outcome_decided_by: {detector_result.outcome_decided_by}"

        # For successful runs, expect PR creation
        if detector_result.task_outcome == "completed_with_pr":
            assert detector_result.pr_number is not None
            assert detector_result.pr_url is not None
            assert detector_result.branch_name is not None
            assert detector_result.permission_denials_count == 0

        # Print evidence location (not credentials)
        print(f"\nE2E Evidence recorded: {evidence_file}")
        print(f"Issue: {issue.url}")
        if detector_result.pr_url:
            print(f"PR: {detector_result.pr_url}")

    finally:
        # Release issue
        tracker.release_issue(issue, agent_id="live-e2e-test")
        evidence["released"] = True
