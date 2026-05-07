"""Tests for the real GitHub tracker adapter (issue #8).

Uses :class:`httpx.MockTransport` so no network is required. Live tests
gated behind ``SYMPHONY_RUN_GITHUB_INTEGRATION=1`` + ``GITHUB_TOKEN``
live in ``tests/test_github_tracker_live.py``.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from symphony.config import GitHubConfig, GitHubProjectConfig, TrackerConfig
from symphony.github import (
    ClaimResult,
    GitHubClient,
    GitHubMissingToken,
    GitHubNotFound,
    GitHubPermissionDenied,
    GitHubRateLimited,
    GitHubTracker,
    GitHubTransportError,
    TrackerError,
    TrackerMissingToken,
    TrackerNotFound,
    TrackerPermissionDenied,
    TrackerRateLimited,
    TrackerTransportError,
    expected_branch_name,
    find_linked_pull_requests,
)
from symphony.models import Issue

# -- Fixtures ----------------------------------------------------------------


def _tracker_config(**overrides: Any) -> TrackerConfig:
    base = {
        "kind": "github",
        "owner": "acme",
        "repo": "proj",
        "token": "ghp_fake_test_token",
        "include_labels": ("symphony-ready",),
        "exclude_labels": ("symphony-blocked",),
    }
    base.update(overrides)
    return TrackerConfig(**base)


def _github_config(**overrides: Any) -> GitHubConfig:
    base: dict[str, Any] = {
        "claim_label": "symphony-running",
        "ready_label": "symphony-ready",
        "blocked_label": "symphony-blocked",
        "done_label": "symphony-done",
        "branch_prefix": "symphony",
        "base_branch": "main",
        "draft_pr": True,
        "claim_comment": True,
        "pr_link_comment": True,
        "close_issue_on_done": False,
        "project": GitHubProjectConfig(),
    }
    base.update(overrides)
    return GitHubConfig(**base)


def _issue(
    *,
    number: int = 42,
    state: str = "open",
    labels: tuple[str, ...] = ("symphony-ready",),
) -> Issue:
    return Issue(
        id=f"I_{number}",
        number=number,
        identifier=f"acme/proj#{number}",
        owner="acme",
        repo="proj",
        title=f"Issue {number}",
        body="body",
        state=state,
        url=f"https://github.com/acme/proj/issues/{number}",
        labels=labels,
    )


def _make_client(handler: Callable[[httpx.Request], httpx.Response]) -> GitHubClient:
    transport = httpx.MockTransport(handler)
    return GitHubClient("ghp_fake_test_token", transport=transport)


def _make_tracker(handler: Callable[[httpx.Request], httpx.Response]) -> GitHubTracker:
    return GitHubTracker(
        _tracker_config(),
        _github_config(),
        client=_make_client(handler),
    )


# -- Client: error mapping ---------------------------------------------------


def test_client_rejects_empty_token() -> None:
    with pytest.raises(GitHubMissingToken):
        GitHubClient("")


def test_client_maps_401_to_missing_token() -> None:
    def h(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "Bad credentials"})

    with _make_client(h) as c, pytest.raises(GitHubMissingToken):
        c.get("/repos/acme/proj/issues")


def test_client_maps_403_to_permission_denied() -> None:
    def h(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"message": "Forbidden"})

    with _make_client(h) as c, pytest.raises(GitHubPermissionDenied):
        c.get("/repos/acme/proj/issues")


def test_client_maps_404_to_not_found() -> None:
    def h(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "Not Found"})

    with _make_client(h) as c, pytest.raises(GitHubNotFound):
        c.get("/repos/acme/proj/issues/9999")


def test_client_maps_429_to_rate_limited_with_retry_after() -> None:
    def h(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            headers={"retry-after": "30"},
            json={"message": "rate limited"},
        )

    with _make_client(h) as c:
        with pytest.raises(GitHubRateLimited) as excinfo:
            c.get("/repos/acme/proj/issues")
        assert excinfo.value.retry_after == 30.0


def test_client_maps_secondary_rate_limit_403_to_rate_limited() -> None:
    def h(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            headers={"x-ratelimit-remaining": "0"},
            json={"message": "API rate limit exceeded"},
        )

    with _make_client(h) as c, pytest.raises(GitHubRateLimited):
        c.get("/repos/acme/proj/issues")


def test_client_maps_transport_error() -> None:
    def h(_req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    with _make_client(h) as c, pytest.raises(GitHubTransportError):
        c.get("/repos/acme/proj/issues")


# -- fetch_candidate_issues --------------------------------------------------


def test_fetch_candidate_issues_filters_prs_and_excluded_labels() -> None:
    requests_seen: list[httpx.Request] = []

    def h(req: httpx.Request) -> httpx.Response:
        requests_seen.append(req)
        return httpx.Response(
            200,
            json=[
                # eligible
                {
                    "id": 1,
                    "node_id": "I_1",
                    "number": 1,
                    "title": "ok",
                    "body": "x",
                    "state": "open",
                    "html_url": "u1",
                    "labels": [{"name": "symphony-ready"}],
                    "assignees": [],
                },
                # PR (has pull_request key) — must be filtered
                {
                    "id": 2,
                    "number": 2,
                    "title": "a pr",
                    "state": "open",
                    "labels": [{"name": "symphony-ready"}],
                    "pull_request": {"url": "..."},
                },
                # excluded label
                {
                    "id": 3,
                    "number": 3,
                    "title": "blocked",
                    "state": "open",
                    "labels": [
                        {"name": "symphony-ready"},
                        {"name": "symphony-blocked"},
                    ],
                    "assignees": [],
                },
                # already claimed
                {
                    "id": 4,
                    "number": 4,
                    "title": "claimed",
                    "state": "open",
                    "labels": [
                        {"name": "symphony-ready"},
                        {"name": "symphony-running"},
                    ],
                    "assignees": [],
                },
            ],
        )

    tracker = _make_tracker(h)
    issues = tracker.fetch_candidate_issues()
    assert [i.number for i in issues] == [1]
    # Verify include_labels passed server-side.
    assert requests_seen[0].url.params.get("labels") == "symphony-ready"


def test_fetch_candidate_issues_handles_empty_list() -> None:
    tracker = _make_tracker(lambda _r: httpx.Response(200, json=[]))
    assert tracker.fetch_candidate_issues() == []


def test_fetch_candidate_issues_wraps_transport_error() -> None:
    def h(_req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("nope")

    tracker = _make_tracker(h)
    with pytest.raises(TrackerError):
        tracker.fetch_candidate_issues()


# -- fetch_issues_by_numbers -------------------------------------------------


def test_fetch_issues_by_numbers_skips_404_and_prs() -> None:
    def h(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/issues/1"):
            return httpx.Response(
                200,
                json={
                    "id": 1,
                    "number": 1,
                    "title": "ok",
                    "state": "open",
                    "labels": [],
                    "assignees": [],
                },
            )
        if req.url.path.endswith("/issues/2"):
            return httpx.Response(404, json={"message": "Not Found"})
        if req.url.path.endswith("/issues/3"):
            return httpx.Response(
                200,
                json={
                    "id": 3,
                    "number": 3,
                    "title": "is a pr",
                    "state": "open",
                    "pull_request": {"url": "..."},
                    "labels": [],
                    "assignees": [],
                },
            )
        return httpx.Response(404)

    tracker = _make_tracker(h)
    out = tracker.fetch_issues_by_numbers([1, 2, 3])
    assert [i.number for i in out] == [1]


# -- claim / release ---------------------------------------------------------


def test_claim_issue_happy_path() -> None:
    posts: list[tuple[str, dict]] = []

    def h(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and req.url.path.endswith("/issues/42"):
            return httpx.Response(
                200,
                json={
                    "id": 42,
                    "number": 42,
                    "title": "x",
                    "state": "open",
                    "labels": [{"name": "symphony-ready"}],
                    "assignees": [],
                },
            )
        if req.method == "POST" and req.url.path.endswith("/issues/42/labels"):
            posts.append(("labels", json.loads(req.content)))
            return httpx.Response(
                200, json=[{"name": "symphony-ready"}, {"name": "symphony-running"}]
            )
        if req.method == "POST" and req.url.path.endswith("/issues/42/comments"):
            posts.append(("comment", json.loads(req.content)))
            return httpx.Response(201, json={"id": 999, "body": "..."})
        return httpx.Response(404)

    tracker = _make_tracker(h)
    result = tracker.claim_issue(_issue(), {"run_id": "run-x", "started_at": "now"})
    assert result == ClaimResult(ok=True)
    assert posts[0] == ("labels", {"labels": ["symphony-running"]})
    assert posts[1][0] == "comment"
    assert "run-x" in posts[1][1]["body"]


def test_claim_issue_detects_existing_claim_label() -> None:
    def h(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            return httpx.Response(
                200,
                json={
                    "id": 42,
                    "number": 42,
                    "state": "open",
                    "labels": [{"name": "symphony-running"}],
                    "assignees": [],
                },
            )
        return httpx.Response(500)  # should not be reached

    tracker = _make_tracker(h)
    result = tracker.claim_issue(_issue(), {"run_id": "run-y"})
    assert result.ok is False
    assert result.conflict is True


def test_claim_issue_422_becomes_claim_conflict() -> None:
    """422 from the labels POST (e.g. label removed mid-claim) becomes a
    soft conflict, not a TrackerError."""

    def h(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            return httpx.Response(
                200,
                json={
                    "id": 42,
                    "number": 42,
                    "state": "open",
                    "labels": [{"name": "symphony-ready"}],
                    "assignees": [],
                },
            )
        if req.method == "POST" and "labels" in req.url.path:
            return httpx.Response(422, json={"message": "validation"})
        return httpx.Response(404)

    tracker = _make_tracker(h)
    result = tracker.claim_issue(_issue(), {"run_id": "run-z"})
    assert result.ok is False
    assert result.conflict is True


def test_claim_issue_succeeds_even_if_comment_post_fails() -> None:
    def h(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            return httpx.Response(
                200,
                json={
                    "id": 42,
                    "number": 42,
                    "state": "open",
                    "labels": [{"name": "symphony-ready"}],
                    "assignees": [],
                },
            )
        if req.method == "POST" and req.url.path.endswith("/labels"):
            return httpx.Response(200, json=[])
        if req.method == "POST" and req.url.path.endswith("/comments"):
            return httpx.Response(500, json={"message": "boom"})
        return httpx.Response(404)

    tracker = _make_tracker(h)
    result = tracker.claim_issue(_issue(), {"run_id": "rid"})
    assert result.ok is True


def test_release_issue_deletes_label() -> None:
    deletes: list[str] = []

    def h(req: httpx.Request) -> httpx.Response:
        if req.method == "DELETE":
            deletes.append(req.url.path)
            return httpx.Response(200, json=[])
        return httpx.Response(500)

    tracker = _make_tracker(h)
    result = tracker.release_issue(_issue(), reason="done")
    assert result.ok is True
    assert deletes == ["/repos/acme/proj/issues/42/labels/symphony-running"]


def test_release_issue_swallows_404_on_missing_label() -> None:
    def h(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "Label does not exist"})

    tracker = _make_tracker(h)
    result = tracker.release_issue(_issue(), reason="r")
    assert result.ok is True


def test_release_issue_wraps_other_errors() -> None:
    def h(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"message": "Forbidden"})

    tracker = _make_tracker(h)
    with pytest.raises(TrackerPermissionDenied) as excinfo:
        tracker.release_issue(_issue(), reason="r")
    assert excinfo.value.status_code == 403


# -- Tracker error categorization (review F1) --------------------------------


@pytest.mark.parametrize(
    "response_factory, expected_type, expected_status",
    [
        (
            lambda: httpx.Response(401, json={"message": "Bad credentials"}),
            TrackerMissingToken,
            401,
        ),
        (
            lambda: httpx.Response(403, json={"message": "Forbidden"}),
            TrackerPermissionDenied,
            403,
        ),
        (
            lambda: httpx.Response(
                429,
                headers={"retry-after": "30"},
                json={"message": "rate limited"},
            ),
            TrackerRateLimited,
            429,
        ),
        (
            lambda: httpx.Response(
                403,
                headers={"x-ratelimit-remaining": "0"},
                json={"message": "secondary rate limit"},
            ),
            TrackerRateLimited,
            403,
        ),
        (
            lambda: httpx.Response(500, json={"message": "boom"}),
            TrackerError,  # generic — no SPEC §9.4 category for 5xx
            500,
        ),
    ],
)
def test_tracker_preserves_error_categories_through_wrap(
    response_factory, expected_type, expected_status
) -> None:
    """SPEC §9.4 + #23 review F1: tracker boundary must distinguish
    error categories (missing_token / permission_denied / not_found /
    rate_limit / transport / malformed / claim_conflict) so the
    orchestrator can branch without parsing strings.
    """

    def h(_req: httpx.Request) -> httpx.Response:
        return response_factory()

    tracker = _make_tracker(h)
    with pytest.raises(expected_type) as excinfo:
        tracker.fetch_candidate_issues()
    assert excinfo.value.status_code == expected_status
    # Subclasses must remain TrackerError so callers that catch the base
    # class still work.
    assert isinstance(excinfo.value, TrackerError)


def test_tracker_rate_limited_preserves_retry_after() -> None:
    def h(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            headers={"retry-after": "45"},
            json={"message": "slow down"},
        )

    tracker = _make_tracker(h)
    with pytest.raises(TrackerRateLimited) as excinfo:
        tracker.fetch_candidate_issues()
    assert excinfo.value.retry_after == 45.0


def test_transport_error_at_tracker_boundary_is_typed() -> None:
    def h(_req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("nope")

    tracker = _make_tracker(h)
    with pytest.raises(TrackerTransportError) as excinfo:
        tracker.fetch_candidate_issues()
    # status_code is None for transport-side failures.
    assert excinfo.value.status_code is None


def test_not_found_propagates_only_outside_per_method_handlers() -> None:
    """`fetch_issues_by_numbers` and `release_issue` swallow 404 (issue
    deleted / label gone). Other methods must surface TrackerNotFound."""

    def h(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "Not Found"})

    tracker = _make_tracker(h)
    # claim_issue's first GET (re-fetch for race check) hits 404.
    with pytest.raises(TrackerNotFound):
        tracker.claim_issue(_issue(), {"run_id": "r"})


# -- mark_issue_blocked ------------------------------------------------------


def test_mark_issue_blocked_adds_label_and_drops_claim() -> None:
    posts: list[dict] = []
    deletes: list[str] = []

    def h(req: httpx.Request) -> httpx.Response:
        if req.method == "POST":
            posts.append(json.loads(req.content))
            return httpx.Response(200, json=[])
        if req.method == "DELETE":
            deletes.append(req.url.path)
            return httpx.Response(200, json=[])
        return httpx.Response(500)

    tracker = _make_tracker(h)
    result = tracker.mark_issue_blocked(_issue(), reason="non-retryable")
    assert result.ok is True
    assert posts == [{"labels": ["symphony-blocked"]}]
    assert deletes == ["/repos/acme/proj/issues/42/labels/symphony-running"]


# -- find_linked_pull_requests ----------------------------------------------


def test_expected_branch_name_uses_prefix_owner_repo_number() -> None:
    cfg = _github_config()
    issue = _issue(number=7)
    assert expected_branch_name(cfg, issue) == "symphony/acme-proj-7"


def test_find_linked_pull_requests_returns_normalized_pr() -> None:
    def h(req: httpx.Request) -> httpx.Response:
        # Verify head filter is correct.
        assert req.url.params.get("head") == "acme:symphony/acme-proj-42"
        assert req.url.params.get("state") == "open"
        return httpx.Response(
            200,
            json=[
                {
                    "id": 100,
                    "node_id": "PR_100",
                    "number": 7,
                    "title": "Fix",
                    "html_url": "https://x/pulls/7",
                    "state": "open",
                    "draft": True,
                    "head": {
                        "ref": "symphony/acme-proj-42",
                        "repo": {
                            "name": "proj",
                            "owner": {"login": "acme"},
                        },
                    },
                    "base": {"ref": "main", "repo": {"default_branch": "main"}},
                    "body": "Fixes #42",
                }
            ],
        )

    client = _make_client(h)
    out = find_linked_pull_requests(client, _issue(), _github_config())
    assert len(out) == 1
    pr = out[0]
    assert pr.number == 7
    assert pr.head_ref == "symphony/acme-proj-42"
    assert pr.base_ref == "main"
    assert pr.is_draft is True
    assert pr.linked_issue_identifier == "#42"


def test_find_linked_pull_requests_handles_404_as_empty() -> None:
    def h(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "Not Found"})

    client = _make_client(h)
    assert find_linked_pull_requests(client, _issue(), _github_config()) == []


# -- comments ----------------------------------------------------------------


def test_create_or_update_pr_link_comment_posts_url() -> None:
    posted: list[str] = []

    def h(req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and req.url.path.endswith("/comments"):
            posted.append(json.loads(req.content)["body"])
            return httpx.Response(201, json={})
        return httpx.Response(404)

    tracker = _make_tracker(h)
    from symphony.models import PullRequest

    pr = PullRequest(
        id="PR_1",
        number=5,
        owner="acme",
        repo="proj",
        title="t",
        url="https://github.com/acme/proj/pull/5",
        state="open",
        head_ref="symphony/acme-proj-42",
        base_ref="main",
    )
    tracker.create_or_update_pr_link_comment(_issue(), pr)
    assert "https://github.com/acme/proj/pull/5" in posted[0]


# -- protocol conformance ----------------------------------------------------


def test_github_tracker_is_a_tracker_protocol() -> None:
    """Structural typing check — the orchestrator depends on the Protocol."""
    # We cannot use isinstance because TrackerProtocol is not runtime-checkable
    # by design (the protocol uses ... bodies). Verify via attribute presence.
    tracker = _make_tracker(lambda _r: httpx.Response(200, json=[]))
    for name in (
        "fetch_candidate_issues",
        "fetch_issues_by_numbers",
        "claim_issue",
        "release_issue",
        "mark_issue_blocked",
        "find_linked_pull_requests",
        "create_or_update_progress_comment",
        "create_or_update_pr_link_comment",
    ):
        assert callable(getattr(tracker, name)), f"missing TrackerProtocol method: {name}"
