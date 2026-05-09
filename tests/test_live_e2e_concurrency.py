"""Opt-in live multi-issue concurrency E2E harness (#167 / M9.3).

Skipped by default. Enabled when ALL of:

- ``SYMPHONY_RUN_CONCURRENCY_E2E=1``
- ``GITHUB_TOKEN`` is non-empty
- ``SYMPHONY_CONCURRENCY_E2E_ISSUES`` lists at least two issue numbers
- The ``claude`` CLI is on ``PATH`` and authenticated
- ``claude-agent-sdk`` is importable

The default CI test in this module is offline. It verifies the harness
configuration contract so the live path cannot silently drift behind the
skip gate.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

import pytest

from symphony.config import build_config
from symphony.github import GitHubTracker
from symphony.models import Issue
from symphony.orchestrator import Orchestrator, WorkerState
from symphony.provider import ClaudeCodeProvider
from symphony.workspace import GitWorkspacePopulator, WorkspaceManager

_GATE_ENV = "SYMPHONY_RUN_CONCURRENCY_E2E"
_ISSUES_ENV = "SYMPHONY_CONCURRENCY_E2E_ISSUES"
_REQUIRE_PR_ENV = "SYMPHONY_CONCURRENCY_E2E_REQUIRE_PR"
_DEFAULT_MODEL = "claude-opus-4-7"


def _gate() -> list[int]:
    if os.environ.get(_GATE_ENV) != "1":
        pytest.skip(f"{_GATE_ENV} not set; concurrency E2E skipped")
    if not os.environ.get("GITHUB_TOKEN"):
        pytest.skip("GITHUB_TOKEN not set; concurrency E2E skipped")
    if shutil.which("claude") is None:
        pytest.skip("`claude` CLI not on PATH; concurrency E2E skipped")
    try:
        import claude_agent_sdk  # noqa: F401
    except ImportError:
        pytest.skip("claude-agent-sdk not installed; concurrency E2E skipped")
    return _issue_numbers(required=True)


def _issue_numbers(*, required: bool) -> list[int]:
    raw = os.environ.get(_ISSUES_ENV, "")
    numbers: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            numbers.append(int(part))
        except ValueError:
            pytest.fail(f"{_ISSUES_ENV} contains non-integer value {part!r}")
    if required and len(numbers) < 2:
        pytest.skip(f"{_ISSUES_ENV} must contain at least two issue numbers")
    return numbers


def _require_completed_with_pr() -> bool:
    """Return whether concurrency E2E must prove PR-producing completion."""
    return os.environ.get(_REQUIRE_PR_ENV) == "1"


def _config(tmp_path: Path):
    owner = os.environ.get("SYMPHONY_GITHUB_TEST_OWNER", "jimoosciuc")
    repo = os.environ.get("SYMPHONY_GITHUB_TEST_REPO", "symphony-cc")
    return build_config(
        {
            "tracker": {
                "kind": "github",
                "owner": owner,
                "repo": repo,
                "token": os.environ.get("GITHUB_TOKEN", "offline-token"),
                "include_labels": ["symphony-ready"],
            },
            "agent": {
                "provider": "claude_code",
                "max_concurrency": 2,
                "max_turns": 1,
            },
            "workspace": {
                "root": str(tmp_path / "workspaces"),
                "populate": "git",
            },
            "claude": {
                "model": os.environ.get("SYMPHONY_CLAUDE_TEST_MODEL", _DEFAULT_MODEL),
                "permission_mode": os.environ.get(
                    "SYMPHONY_CONCURRENCY_E2E_PERMISSION_MODE",
                    "acceptEdits",
                ),
                "session_store": str(tmp_path / "sessions"),
                "transcript_store": str(tmp_path / "transcripts"),
                "artifact_store": str(tmp_path / "artifacts"),
                "turn_timeout_ms": 300_000,
                "stall_timeout_ms": 120_000,
            },
            "github": {},
            "logging": {
                "redact_keys": ["token", "authorization", "api_key", "password", "secret"]
            },
        },
        workflow_path=tmp_path / "WORKFLOW.md",
    )


def test_concurrency_e2e_harness_contract(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SYMPHONY_CLAUDE_TEST_MODEL", raising=False)
    monkeypatch.delenv(_REQUIRE_PR_ENV, raising=False)
    monkeypatch.setenv(_ISSUES_ENV, "101,102")
    config = _config(tmp_path)
    numbers = _issue_numbers(required=True)

    assert numbers == [101, 102]
    assert config.agent.max_concurrency == 2
    assert config.claude.model == _DEFAULT_MODEL
    assert config.workspace.populate == "git"
    assert "token" in config.logging.redact_keys
    assert _require_completed_with_pr() is False


def test_concurrency_e2e_can_require_completed_with_pr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(_ISSUES_ENV, "101,102")
    monkeypatch.setenv(_REQUIRE_PR_ENV, "1")
    monkeypatch.setenv("SYMPHONY_CONCURRENCY_E2E_PERMISSION_MODE", "bypassPermissions")

    config = _config(tmp_path)

    assert config.claude.permission_mode == "bypassPermissions"
    assert _require_completed_with_pr() is True


def test_concurrency_pr_required_assertion_reads_terminal_evidence(tmp_path: Path) -> None:
    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir()
    (artifact_dir / "terminal.json").write_text(
        json.dumps(
            {
                "task_outcome": "completed_with_pr",
                "task_evidence": [
                    {
                        "type": "pr_linked",
                        "number": 198,
                        "url": "https://github.com/jimoosciuc/symphony-cc/pull/198",
                        "head_ref": "symphony/test",
                    }
                ],
            }
        )
    )

    summaries = _finished_pr_summaries(
        [
            {
                "issue_identifier": "jimoosciuc/symphony-cc#198",
                "artifact_dir": str(artifact_dir),
                "task_outcome": "completed_with_pr",
            }
        ]
    )

    _assert_completed_with_pr(summaries)
    assert summaries == [
        {
            "issue_identifier": "jimoosciuc/symphony-cc#198",
            "terminal_json_path": str(artifact_dir / "terminal.json"),
            "task_outcome": "completed_with_pr",
            "pr_number": 198,
            "pr_url": "https://github.com/jimoosciuc/symphony-cc/pull/198",
            "branch_name": "symphony/test",
        }
    ]


def test_concurrency_pr_required_assertion_fails_without_pr(tmp_path: Path) -> None:
    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir()
    (artifact_dir / "terminal.json").write_text(
        json.dumps({"task_outcome": "completed_no_pr_declared", "task_evidence": []})
    )
    summaries = _finished_pr_summaries(
        [
            {
                "issue_identifier": "jimoosciuc/symphony-cc#199",
                "artifact_dir": str(artifact_dir),
                "task_outcome": "completed_no_pr_declared",
            }
        ]
    )

    with pytest.raises(AssertionError, match="completed_with_pr"):
        _assert_completed_with_pr(summaries)


async def test_live_multi_issue_concurrency(tmp_path: Path) -> None:
    numbers = _gate()
    config = _config(tmp_path)
    require_completed_with_pr = _require_completed_with_pr()
    base_tracker = GitHubTracker(config.tracker, config.github)
    issues = base_tracker.fetch_issues_by_numbers(numbers[: config.agent.max_concurrency])
    found = {issue.number for issue in issues}
    missing = [number for number in numbers[: config.agent.max_concurrency] if number not in found]
    if missing:
        pytest.fail(f"Configured concurrency issue(s) not found: {missing}")

    tracker = _SelectedIssueTracker(base_tracker, issues)
    provider = ClaudeCodeProvider()
    workspace_manager = WorkspaceManager(
        config.workspace,
        populator=GitWorkspacePopulator(config.tracker, config.github),
    )
    orchestrator = Orchestrator(
        config,
        tracker=tracker,
        provider=provider,
        workspace_manager=workspace_manager,
        continuation_policy=_live_prompt,
    )

    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    try:
        result = await orchestrator.run_once()
    finally:
        base_tracker.client.close()

    expected = {issue.identifier for issue in issues}
    finished_pr_summaries = _finished_pr_summaries(orchestrator.recent_finished)
    evidence = {
        "issue_urls": {issue.identifier: issue.url for issue in issues},
        "permission_mode": config.claude.permission_mode,
        "require_completed_with_pr": require_completed_with_pr,
        "dispatched": result.dispatched,
        "finished": result.finished,
        "retries_scheduled": result.retries_scheduled,
        "skipped_claim_conflict": result.skipped_claim_conflict,
        "status": orchestrator.status_snapshot(),
        "recent_finished": orchestrator.recent_finished,
        "finished_pr_summaries": finished_pr_summaries,
    }
    evidence_file = evidence_dir / "concurrency_e2e_evidence.json"
    evidence_file.write_text(json.dumps(evidence, indent=2, sort_keys=True))

    assert set(result.dispatched) == expected
    assert set(result.finished).issubset(expected)
    assert len(orchestrator.recent_finished) == len(result.finished)
    _assert_isolated_finished(orchestrator.recent_finished)
    if require_completed_with_pr:
        assert set(result.finished) == expected
        _assert_completed_with_pr(finished_pr_summaries)


def _live_prompt(worker: WorkerState) -> str | None:
    if worker.turn_count > 0:
        return None
    return (
        f"Work on GitHub issue {worker.issue.identifier}: {worker.issue.title}\n\n"
        f"{worker.issue.body}\n\n"
        "Keep changes scoped to this issue. If you create a PR, link it to the issue."
    )


def _assert_isolated_finished(items: list[dict[str, Any]]) -> None:
    artifact_dirs = [item["artifact_dir"] for item in items]
    session_ids = [item["session_id"] for item in items]
    issue_ids = [item["issue_identifier"] for item in items]
    assert len(artifact_dirs) == len(set(artifact_dirs))
    assert len(session_ids) == len(set(session_ids))
    assert len(issue_ids) == len(set(issue_ids))


def _finished_pr_summaries(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    for item in items:
        terminal_path = Path(item["artifact_dir"]) / "terminal.json"
        terminal_data: dict[str, Any] = {}
        if terminal_path.exists():
            terminal_data = json.loads(terminal_path.read_text())

        pr_number = None
        pr_url = None
        branch_name = None
        for entry in terminal_data.get("task_evidence", []):
            if entry.get("type") == "pr_linked" and pr_number is None:
                pr_number = entry.get("number")
                pr_url = entry.get("url")
                branch_name = entry.get("head_ref")
            if entry.get("type") == "branch_pushed" and branch_name is None:
                branch_name = entry.get("name")

        summaries.append(
            {
                "issue_identifier": item["issue_identifier"],
                "terminal_json_path": str(terminal_path),
                "task_outcome": terminal_data.get(
                    "task_outcome",
                    item.get("task_outcome"),
                ),
                "pr_number": pr_number,
                "pr_url": pr_url,
                "branch_name": branch_name,
            }
        )
    return summaries


def _assert_completed_with_pr(summaries: list[dict[str, Any]]) -> None:
    failures = [
        summary
        for summary in summaries
        if summary.get("task_outcome") != "completed_with_pr"
        or not summary.get("pr_number")
        or not summary.get("pr_url")
    ]
    assert not failures, (
        "Concurrency E2E required completed_with_pr for every finished issue; "
        f"failures={failures}"
    )


class _SelectedIssueTracker:
    def __init__(self, base: GitHubTracker, issues: list[Issue]) -> None:
        self._base = base
        self._issues = issues

    def fetch_candidate_issues(self) -> list[Issue]:
        return list(self._issues)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._base, name)
