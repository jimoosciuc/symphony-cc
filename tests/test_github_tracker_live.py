"""Opt-in live integration tests for the real GitHub tracker.

Skipped by default. Enabled when both:

- ``SYMPHONY_RUN_GITHUB_INTEGRATION=1``
- ``GITHUB_TOKEN`` is non-empty.

The default test repo is ``jimoosciuc/symphony-cc``; override with
``SYMPHONY_GITHUB_TEST_OWNER`` and ``SYMPHONY_GITHUB_TEST_REPO`` for
private targets. These tests perform read-only API calls against the
configured repo:

- list candidate issues with the project's own ``symphony-ready`` label
- fetch one issue by number
- list linked PRs (best-effort; usually empty)

No write operations are performed in this file. Write-side claim/
release behavior is exercised in fake-tier tests and will be re-tested
end-to-end during the M3 #11 issue-driven dry run.
"""

from __future__ import annotations

import os

import pytest

from symphony.config import GitHubConfig, GitHubProjectConfig, TrackerConfig
from symphony.github import GitHubTracker

_GATE_ENV = "SYMPHONY_RUN_GITHUB_INTEGRATION"


def _gate() -> None:
    if os.environ.get(_GATE_ENV) != "1":
        pytest.skip(f"{_GATE_ENV} not set; live GitHub tests skipped")
    if not os.environ.get("GITHUB_TOKEN"):
        pytest.skip("GITHUB_TOKEN not set; live GitHub tests skipped")


@pytest.fixture
def live_tracker() -> GitHubTracker:
    _gate()
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
    return GitHubTracker(tracker, github)


def test_live_fetch_candidate_issues(live_tracker: GitHubTracker) -> None:
    issues = live_tracker.fetch_candidate_issues()
    # Don't assert non-empty (the repo may legitimately have zero
    # candidates) — assert shape.
    for issue in issues:
        assert issue.owner == live_tracker._owner
        assert issue.repo == live_tracker._repo
        assert issue.number > 0


def test_live_fetch_issue_by_number(live_tracker: GitHubTracker) -> None:
    # Issue #1 exists in symphony-cc; if running against another repo
    # the test will simply find no issue and pass.
    out = live_tracker.fetch_issues_by_numbers([1])
    if out:
        assert out[0].number == 1


def test_live_find_linked_prs_does_not_error(live_tracker: GitHubTracker) -> None:
    # Pick whatever exists; if there are no candidates, fall through.
    candidates = live_tracker.fetch_candidate_issues()
    if not candidates:
        pytest.skip("no candidate issues available for linked-PR query")
    prs = live_tracker.find_linked_pull_requests(candidates[0])
    # Just shape — may be empty.
    for pr in prs:
        assert pr.number > 0
