"""GitHub pull-request coordination.

Symphony's MVP uses the **agent-managed PR strategy** (SPEC §13): the
Claude Code session is responsible for creating commits, pushing the
branch, and opening / updating the pull request. This module is the
*read-side* boundary the orchestrator uses to:

- Discover the linked PR for a given issue.
- Compute the expected branch name so prompts and reconciliation can
  reference it consistently.

The active *write-side* (`ensure_branch`, `ensure_pull_request`) is
intentionally **not** implemented here for the MVP — it lives in the
Claude prompt. Wiring a Symphony-managed PR strategy is a follow-up
issue (the symphony_managed_pr branch of SPEC §13) and would land in
this module when chosen.
"""

from __future__ import annotations

import re

from symphony.config import GitHubConfig
from symphony.github.client import GitHubClient, GitHubNotFound
from symphony.models import Issue, PullRequest


def expected_branch_name(config: GitHubConfig, issue: Issue) -> str:
    """Compute the deterministic branch name Symphony asks the agent to use.

    SPEC §8 default: ``<github.branch_prefix>/<owner>-<repo>-<issue_number>``.
    Used both by the rendered first prompt (so Claude pushes to a
    predictable branch) and by :func:`find_linked_pull_requests` (so the
    GitHub query can target the expected head ref).
    """
    return f"{config.branch_prefix}/{issue.owner}-{issue.repo}-{issue.number}"


def find_linked_pull_requests(
    client: GitHubClient,
    issue: Issue,
    config: GitHubConfig,
) -> list[PullRequest]:
    """Return open PRs whose head branch matches Symphony's naming convention.

    GitHub's REST list-pulls endpoint accepts a ``head`` filter of the form
    ``owner:branch``. This is sufficient for agent-managed PRs because
    Symphony controls the branch name via the rendered prompt. Closed PRs
    are excluded — the orchestrator only cares about a *currently open*
    linked PR for reconciliation.

    Returns an empty list (not 404) when the agent hasn't pushed a branch
    yet. Other GitHub errors propagate; the caller catches at the tracker
    boundary.
    """
    head = f"{issue.owner}:{expected_branch_name(config, issue)}"
    try:
        raw = client.get(
            f"/repos/{issue.owner}/{issue.repo}/pulls",
            params={"head": head, "state": "open"},
        )
    except GitHubNotFound:
        return []
    return [_normalize_pull_request(pr, issue.identifier) for pr in raw or []]


# -- Internals ----------------------------------------------------------------


# Capture the linked-issue identifier from "Fixes #123" / "Closes
# owner/repo#123" mentions in the PR body. SPEC §13 says the PR body
# SHOULD include such a reference; we use it as a hint when the head-ref
# lookup didn't already prove the link.
_ISSUE_REF = re.compile(
    r"(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+"
    r"(?P<id>(?:[\w.-]+/[\w.-]+)?#\d+)",
    re.IGNORECASE,
)


def _normalize_pull_request(raw: dict, fallback_identifier: str) -> PullRequest:
    head = raw.get("head", {}) or {}
    base = raw.get("base", {}) or {}
    head_repo = head.get("repo") or {}
    base_repo = base.get("repo") or {}
    body = raw.get("body") or ""
    match = _ISSUE_REF.search(body)
    linked = match.group("id") if match else fallback_identifier
    return PullRequest(
        id=str(raw.get("node_id") or raw.get("id") or ""),
        number=int(raw["number"]),
        owner=str(head_repo.get("owner", {}).get("login") or ""),
        repo=str(head_repo.get("name") or ""),
        title=str(raw.get("title") or ""),
        url=str(raw.get("html_url") or ""),
        state=str(raw.get("state") or "open"),
        head_ref=str(head.get("ref") or ""),
        base_ref=str(base.get("ref") or base_repo.get("default_branch") or ""),
        is_draft=bool(raw.get("draft", False)),
        mergeable_state=raw.get("mergeable_state"),
        linked_issue_identifier=linked,
        raw=raw,
    )
