"""Opt-in live runtime-lanes E2E harness (#204 / M10.10).

Skipped by default. Enabled when ALL of:

- ``SYMPHONY_RUN_LANES_E2E=1``
- ``GITHUB_TOKEN`` is non-empty
- ``SYMPHONY_LANES_E2E_ISSUES`` lists at least two issue numbers
- The ``claude`` CLI is on ``PATH`` and authenticated
- ``claude-agent-sdk`` is importable

Default CI only verifies the offline harness contract. The live path proves
that real GitHub issues are routed into distinct runtime lanes, receive
lane-specific prompts, and produce terminal evidence.
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

_GATE_ENV = "SYMPHONY_RUN_LANES_E2E"
_ISSUES_ENV = "SYMPHONY_LANES_E2E_ISSUES"
_REQUIRE_PR_ENV = "SYMPHONY_LANES_E2E_REQUIRE_PR"
_PERMISSION_MODE_ENV = "SYMPHONY_LANES_E2E_PERMISSION_MODE"
_DEFAULT_MODEL = "claude-opus-4-7"

_IMPLEMENTER_LABEL = "status:ready-for-implementation"
_REVIEWER_LABEL = "status:ready-for-review"


def _gate() -> list[int]:
    if os.environ.get(_GATE_ENV) != "1":
        pytest.skip(f"{_GATE_ENV} not set; lanes E2E skipped")
    if not os.environ.get("GITHUB_TOKEN"):
        pytest.skip("GITHUB_TOKEN not set; lanes E2E skipped")
    if shutil.which("claude") is None:
        pytest.skip("`claude` CLI not on PATH; lanes E2E skipped")
    try:
        import claude_agent_sdk  # noqa: F401
    except ImportError:
        pytest.skip("claude-agent-sdk not installed; lanes E2E skipped")
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
                    _PERMISSION_MODE_ENV,
                    "acceptEdits",
                ),
                "session_store": str(tmp_path / "sessions"),
                "transcript_store": str(tmp_path / "transcripts"),
                "artifact_store": str(tmp_path / "artifacts"),
                "turn_timeout_ms": 300_000,
                "stall_timeout_ms": 120_000,
            },
            "lanes": [
                {
                    "name": "implementer",
                    "include_labels": [_IMPLEMENTER_LABEL],
                    "exclude_labels": ["do-not-claim", "leader-owned"],
                    "max_concurrency": 1,
                    "prompt_prefix": (
                        "You are the implementer lane. Make only the scoped "
                        "code or documentation change requested by this issue."
                    ),
                    "prompt_suffix": "Open a PR linked to the issue when complete.",
                },
                {
                    "name": "reviewer",
                    "include_labels": [_REVIEWER_LABEL],
                    "exclude_labels": ["do-not-claim", "leader-owned"],
                    "max_concurrency": 1,
                    "prompt_prefix": (
                        "You are the reviewer lane. For this live validation "
                        "task, follow the issue body exactly and keep changes scoped."
                    ),
                    "prompt_suffix": "Open a PR linked to the issue when complete.",
                },
            ],
            "github": {},
            "logging": {
                "redact_keys": ["token", "authorization", "api_key", "password", "secret"]
            },
        },
        workflow_path=tmp_path / "WORKFLOW.md",
    )


def test_lanes_e2e_harness_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SYMPHONY_CLAUDE_TEST_MODEL", raising=False)
    monkeypatch.delenv(_REQUIRE_PR_ENV, raising=False)
    monkeypatch.delenv(_PERMISSION_MODE_ENV, raising=False)
    monkeypatch.setenv(_ISSUES_ENV, "301,302")

    config = _config(tmp_path)
    numbers = _issue_numbers(required=True)

    assert numbers == [301, 302]
    assert config.agent.max_concurrency == 2
    assert config.claude.model == _DEFAULT_MODEL
    assert config.claude.permission_mode == "acceptEdits"
    assert config.workspace.populate == "git"
    assert [lane.name for lane in config.lanes] == ["implementer", "reviewer"]
    assert config.lanes[0].include_labels == (_IMPLEMENTER_LABEL,)
    assert config.lanes[1].include_labels == (_REVIEWER_LABEL,)
    assert _require_completed_with_pr() is False


def test_lanes_e2e_can_require_completed_with_pr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(_ISSUES_ENV, "301,302")
    monkeypatch.setenv(_REQUIRE_PR_ENV, "1")
    monkeypatch.setenv(_PERMISSION_MODE_ENV, "bypassPermissions")

    config = _config(tmp_path)

    assert config.claude.permission_mode == "bypassPermissions"
    assert _require_completed_with_pr() is True


def test_expected_lane_mapping_requires_distinct_lane_labels() -> None:
    issues = [
        _issue(301, labels=("symphony-ready", _IMPLEMENTER_LABEL)),
        _issue(302, labels=("symphony-ready", _REVIEWER_LABEL)),
    ]

    assert _expected_lanes(issues) == {
        "jimoosciuc/symphony-cc#301": "implementer",
        "jimoosciuc/symphony-cc#302": "reviewer",
    }


def test_expected_lane_mapping_rejects_missing_lane_label() -> None:
    with pytest.raises(AssertionError, match="does not match a live lane"):
        _expected_lanes([_issue(303, labels=("symphony-ready",))])


def test_lanes_pr_required_assertion_reads_terminal_evidence(tmp_path: Path) -> None:
    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir()
    (artifact_dir / "terminal.json").write_text(
        json.dumps(
            {
                "task_outcome": "completed_with_pr",
                "task_evidence": [
                    {
                        "type": "pr_linked",
                        "number": 204,
                        "url": "https://github.com/jimoosciuc/symphony-cc/pull/204",
                        "head_ref": "live-lanes/implementer",
                    }
                ],
            }
        )
    )
    summaries = _finished_summaries(
        [
            {
                "issue_identifier": "jimoosciuc/symphony-cc#204",
                "artifact_dir": str(artifact_dir),
                "lane": "implementer",
                "task_outcome": "completed_with_pr",
            }
        ]
    )

    _assert_completed_with_pr(summaries)
    assert summaries[0]["lane"] == "implementer"
    assert summaries[0]["pr_number"] == 204
    assert summaries[0]["branch_name"] == "live-lanes/implementer"


async def test_live_runtime_lanes(tmp_path: Path) -> None:
    numbers = _gate()
    config = _config(tmp_path)
    require_completed_with_pr = _require_completed_with_pr()
    base_tracker = GitHubTracker(config.tracker, config.github)
    issues = base_tracker.fetch_issues_by_numbers(numbers[: config.agent.max_concurrency])
    found = {issue.number for issue in issues}
    missing = [number for number in numbers[: config.agent.max_concurrency] if number not in found]
    if missing:
        pytest.fail(f"Configured lane E2E issue(s) not found: {missing}")

    expected_lanes = _expected_lanes(issues)
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

    finished_summaries = _finished_summaries(orchestrator.recent_finished)
    evidence = {
        "issue_urls": {issue.identifier: issue.url for issue in issues},
        "expected_lanes": expected_lanes,
        "permission_mode": config.claude.permission_mode,
        "require_completed_with_pr": require_completed_with_pr,
        "dispatched": result.dispatched,
        "finished": result.finished,
        "retries_scheduled": result.retries_scheduled,
        "skipped_claim_conflict": result.skipped_claim_conflict,
        "status": orchestrator.status_snapshot(),
        "recent_finished": orchestrator.recent_finished,
        "finished_summaries": finished_summaries,
    }
    evidence_file = evidence_dir / "lanes_e2e_evidence.json"
    evidence_file.write_text(json.dumps(evidence, indent=2, sort_keys=True))

    assert set(result.dispatched) == set(expected_lanes)
    assert set(result.finished).issubset(set(expected_lanes))
    assert len(orchestrator.recent_finished) == len(result.finished)
    _assert_finished_lanes(finished_summaries, expected_lanes)
    if require_completed_with_pr:
        assert set(result.finished) == set(expected_lanes)
        _assert_completed_with_pr(finished_summaries)


def _live_prompt(worker: WorkerState) -> str | None:
    if worker.turn_count > 0:
        return None
    return (
        f"Work on GitHub issue {worker.issue.identifier}: {worker.issue.title}\n\n"
        f"{worker.issue.body}\n\n"
        "Keep changes scoped to this issue. If you create a PR, link it to the issue."
    )


def _issue(number: int, *, labels: tuple[str, ...]) -> Issue:
    return Issue(
        id=f"I_{number}",
        number=number,
        identifier=f"jimoosciuc/symphony-cc#{number}",
        owner="jimoosciuc",
        repo="symphony-cc",
        title=f"lane smoke {number}",
        body="body",
        state="open",
        url=f"https://github.com/jimoosciuc/symphony-cc/issues/{number}",
        labels=labels,
    )


def _expected_lanes(issues: list[Issue]) -> dict[str, str]:
    expected: dict[str, str] = {}
    for issue in issues:
        labels = set(issue.labels)
        if _IMPLEMENTER_LABEL in labels:
            expected[issue.identifier] = "implementer"
        elif _REVIEWER_LABEL in labels:
            expected[issue.identifier] = "reviewer"
        else:
            raise AssertionError(f"{issue.identifier} does not match a live lane label")
    return expected


def _finished_summaries(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
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
                "lane": item.get("lane"),
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


def _assert_finished_lanes(
    summaries: list[dict[str, Any]],
    expected_lanes: dict[str, str],
) -> None:
    actual = {
        summary["issue_identifier"]: summary.get("lane")
        for summary in summaries
    }
    assert actual == {
        issue_identifier: expected_lanes[issue_identifier]
        for issue_identifier in actual
    }


def _assert_completed_with_pr(summaries: list[dict[str, Any]]) -> None:
    failures = [
        summary
        for summary in summaries
        if summary.get("task_outcome") != "completed_with_pr"
        or not summary.get("pr_number")
        or not summary.get("pr_url")
    ]
    assert not failures, (
        "Lanes E2E required completed_with_pr for every finished issue; "
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
