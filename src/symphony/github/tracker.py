"""GitHub tracker boundary, real adapter, and an in-memory fake for tests.

The orchestrator depends on the :class:`TrackerProtocol`. Two
implementations live here:

- :class:`GitHubTracker` — real REST adapter built on
  :class:`~symphony.github.client.GitHubClient`. Used in production.
- :class:`FakeGitHubTracker` — in-memory, for orchestrator tests.

Boundary methods correspond to ``SPEC.md`` §9.1:

- ``fetch_candidate_issues`` — issues eligible to dispatch.
- ``fetch_issues_by_numbers`` — refresh known issues for reconciliation.
- ``claim_issue`` — atomic-enough claim with status report.
- ``release_issue`` — drop the claim after success/failure.
- ``mark_issue_blocked`` — non-retryable failure surface.
- ``find_linked_pull_requests`` — for reconciliation when a linked PR merges.
- ``create_or_update_progress_comment`` / ``create_or_update_pr_link_comment`` —
  operator-visible status surface.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Protocol

from symphony.config import GitHubConfig, TrackerConfig
from symphony.github.client import (
    GitHubClaimConflict,
    GitHubClient,
    GitHubError,
    GitHubMissingToken,
    GitHubNotFound,
    GitHubPermissionDenied,
    GitHubRateLimited,
    GitHubTransportError,
)
from symphony.github.pr import find_linked_pull_requests as _find_linked_prs
from symphony.models import Issue, PullRequest

_LOG = logging.getLogger("symphony.github.tracker")

# -- Errors --------------------------------------------------------------------


class TrackerError(RuntimeError):
    """Generic tracker failure surface.

    Real adapters subclass this with transport / permission / not-found
    flavors; the fake uses the bare class for scripted failures.
    """


# -- Result types --------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ClaimResult:
    """Outcome of :meth:`TrackerProtocol.claim_issue`.

    ``ok=False`` with ``conflict=True`` means another live run already
    owns the issue — the orchestrator should skip it. ``ok=False`` with
    ``conflict=False`` means a transport-level error happened.
    """

    ok: bool
    conflict: bool = False
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class ReleaseResult:
    ok: bool
    reason: str | None = None


# -- Protocol -----------------------------------------------------------------


class TrackerProtocol(Protocol):
    def fetch_candidate_issues(self) -> list[Issue]: ...

    def fetch_issues_by_numbers(self, numbers: list[int]) -> list[Issue]: ...

    def claim_issue(self, issue: Issue, run_metadata: dict[str, Any]) -> ClaimResult: ...

    def release_issue(self, issue: Issue, reason: str) -> ReleaseResult: ...

    def mark_issue_blocked(self, issue: Issue, reason: str) -> ReleaseResult: ...

    def find_linked_pull_requests(self, issue: Issue) -> list[PullRequest]: ...

    def create_or_update_progress_comment(self, issue: Issue, body: str) -> None: ...

    def create_or_update_pr_link_comment(self, issue: Issue, pr: PullRequest) -> None: ...


# -- Real GitHub adapter ------------------------------------------------------


class GitHubTracker:
    """Real REST-backed implementation of :class:`TrackerProtocol`.

    Constructed with the resolved :class:`TrackerConfig` and
    :class:`GitHubConfig` from the workflow loader. Owns one
    :class:`GitHubClient` for the run; the orchestrator wraps each call
    in ``asyncio.to_thread`` so the sync httpx client doesn't block its
    event loop.

    Errors from :mod:`symphony.github.client` are mapped to
    :class:`TrackerError` (or :class:`ClaimResult(conflict=True)` for the
    422-style claim races); the orchestrator's protocol-level handlers
    don't see httpx types.
    """

    def __init__(
        self,
        tracker: TrackerConfig,
        github: GitHubConfig,
        *,
        client: GitHubClient | None = None,
    ) -> None:
        self.tracker = tracker
        self.github = github
        self._client = client or GitHubClient(tracker.token)
        self._owner = tracker.owner
        self._repo = tracker.repo

    @property
    def client(self) -> GitHubClient:
        return self._client

    def close(self) -> None:
        self._client.close()

    # -- Candidate fetch / refresh ---------------------------------------

    def fetch_candidate_issues(self) -> list[Issue]:
        # GitHub's labels filter is AND across the comma-separated set;
        # apply include_labels server-side and re-check exclude_labels
        # client-side because the API has no exclusion knob.
        params: dict[str, Any] = {
            "state": "open",
            "per_page": 100,
            "sort": "updated",
            "direction": "desc",
        }
        if self.tracker.include_labels:
            params["labels"] = ",".join(self.tracker.include_labels)
        try:
            raw = self._client.get(f"/repos/{self._owner}/{self._repo}/issues", params=params)
        except GitHubError as exc:
            raise self._wrap(exc) from exc

        out: list[Issue] = []
        for item in raw or []:
            # The /issues endpoint also returns PRs; SPEC §9 wants issues
            # only, so filter by absence of the `pull_request` key.
            if "pull_request" in item:
                continue
            issue = _normalize_issue(item, owner=self._owner, repo=self._repo)
            if any(lbl in issue.labels for lbl in self.tracker.exclude_labels):
                continue
            if self.github.claim_label in issue.labels:
                # Already claimed by some run; skip server-side too.
                continue
            out.append(issue)
        return out

    def fetch_issues_by_numbers(self, numbers: list[int]) -> list[Issue]:
        out: list[Issue] = []
        for n in numbers:
            try:
                raw = self._client.get(f"/repos/{self._owner}/{self._repo}/issues/{n}")
            except GitHubNotFound:
                continue  # issue deleted; the orchestrator will treat it as ineligible
            except GitHubError as exc:
                raise self._wrap(exc) from exc
            if "pull_request" in raw:
                continue
            out.append(_normalize_issue(raw, owner=self._owner, repo=self._repo))
        return out

    # -- Claim / release --------------------------------------------------

    def claim_issue(self, issue: Issue, run_metadata: dict[str, Any]) -> ClaimResult:
        # Re-fetch to check for a race: another runner may have already
        # added claim_label between fetch_candidate_issues and now.
        try:
            current = self._client.get(f"/repos/{self._owner}/{self._repo}/issues/{issue.number}")
        except GitHubError as exc:
            raise self._wrap(exc) from exc
        existing_labels = {lbl["name"] for lbl in current.get("labels", []) or []}
        if self.github.claim_label in existing_labels:
            return ClaimResult(ok=False, conflict=True, reason="claim_label already present")

        try:
            self._client.post(
                f"/repos/{self._owner}/{self._repo}/issues/{issue.number}/labels",
                json_body={"labels": [self.github.claim_label]},
            )
        except GitHubClaimConflict as exc:
            return ClaimResult(ok=False, conflict=True, reason=str(exc))
        except GitHubError as exc:
            raise self._wrap(exc) from exc

        if self.github.claim_comment:
            body = _claim_comment_body(run_metadata)
            try:
                self._client.post(
                    f"/repos/{self._owner}/{self._repo}/issues/{issue.number}/comments",
                    json_body={"body": body},
                )
            except GitHubError as exc:
                # Claim succeeded but comment failed; SPEC §16 lists this
                # as retryable. Surface the partial state so the
                # orchestrator can decide.
                _LOG.warning(
                    "claim_issue: comment failed after label add for %s: %s",
                    issue.identifier,
                    exc,
                )
                # Don't return ok=False — the label IS the claim. Comment
                # is best-effort observability.
        return ClaimResult(ok=True)

    def release_issue(self, issue: Issue, reason: str) -> ReleaseResult:
        try:
            self._client.delete(
                f"/repos/{self._owner}/{self._repo}/issues/{issue.number}"
                f"/labels/{self.github.claim_label}"
            )
        except GitHubNotFound:
            # Label wasn't there — fine, we're already released.
            pass
        except GitHubError as exc:
            raise self._wrap(exc) from exc
        return ReleaseResult(ok=True, reason=reason)

    def mark_issue_blocked(self, issue: Issue, reason: str) -> ReleaseResult:
        try:
            self._client.post(
                f"/repos/{self._owner}/{self._repo}/issues/{issue.number}/labels",
                json_body={"labels": [self.github.blocked_label]},
            )
        except GitHubError as exc:
            raise self._wrap(exc) from exc
        # Best-effort: also drop the claim label so reconciliation doesn't
        # treat the blocked issue as still owned by this run.
        try:
            self._client.delete(
                f"/repos/{self._owner}/{self._repo}/issues/{issue.number}"
                f"/labels/{self.github.claim_label}"
            )
        except GitHubNotFound:
            pass
        except GitHubError as exc:
            _LOG.warning(
                "mark_issue_blocked: claim label removal failed for %s: %s",
                issue.identifier,
                exc,
            )
        return ReleaseResult(ok=True, reason=reason)

    # -- Linked PRs / comments -------------------------------------------

    def find_linked_pull_requests(self, issue: Issue) -> list[PullRequest]:
        try:
            return _find_linked_prs(self._client, issue, self.github)
        except GitHubError as exc:
            raise self._wrap(exc) from exc

    def create_or_update_progress_comment(self, issue: Issue, body: str) -> None:
        # MVP: append-only. A future iteration may dedupe by Symphony
        # marker (e.g. <!-- symphony:progress -->). Keeping it simple
        # avoids a second list+patch round-trip per call.
        try:
            self._client.post(
                f"/repos/{self._owner}/{self._repo}/issues/{issue.number}/comments",
                json_body={"body": body},
            )
        except GitHubError as exc:
            raise self._wrap(exc) from exc

    def create_or_update_pr_link_comment(self, issue: Issue, pr: PullRequest) -> None:
        body = f"Symphony opened {pr.url} for this issue."
        self.create_or_update_progress_comment(issue, body)

    # -- Internals --------------------------------------------------------

    def _wrap(self, exc: GitHubError) -> TrackerError:
        # Map adapter errors to TrackerError so the protocol stays clean.
        # Sub-types could be exposed if the orchestrator wants to branch
        # on rate-limit vs permission-denied later.
        return TrackerError(str(exc))


# -- Normalization -----------------------------------------------------------


def _normalize_issue(raw: dict, *, owner: str, repo: str) -> Issue:
    labels = tuple(lbl["name"] for lbl in raw.get("labels", []) or [])
    assignees = tuple(a.get("login", "") for a in raw.get("assignees", []) or [])
    return Issue(
        id=str(raw.get("node_id") or raw.get("id") or ""),
        number=int(raw["number"]),
        identifier=f"{owner}/{repo}#{raw['number']}",
        owner=owner,
        repo=repo,
        title=str(raw.get("title") or ""),
        body=str(raw.get("body") or ""),
        state=str(raw.get("state") or "open"),
        url=str(raw.get("html_url") or ""),
        labels=labels,
        assignees=assignees,
        updated_at=_parse_iso(raw.get("updated_at")),
        created_at=_parse_iso(raw.get("created_at")),
        raw=raw,
    )


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    # GitHub returns ``2026-05-07T15:00:00Z``; fromisoformat handles the
    # trailing ``Z`` from Python 3.11+ but the repo targets 3.10, so
    # normalize.
    cleaned = value.replace("Z", "+00:00") if value.endswith("Z") else value
    try:
        return datetime.fromisoformat(cleaned)
    except ValueError:
        return None


def _claim_comment_body(run_metadata: dict[str, Any]) -> str:
    parts = ["Symphony claimed this issue."]
    run_id = run_metadata.get("run_id")
    if run_id:
        parts.append(f"Run id: `{run_id}`.")
    started = run_metadata.get("started_at")
    if started:
        parts.append(f"Started at: {started}.")
    return " ".join(parts)


# Silence unused-import warnings for symbols re-exported via __init__.
_ = (GitHubMissingToken, GitHubPermissionDenied, GitHubRateLimited, GitHubTransportError)


# -- Fake ---------------------------------------------------------------------


@dataclass(slots=True)
class _IssueState:
    """Mutable per-issue state owned by FakeGitHubTracker."""

    issue: Issue
    claimed_by: str | None = None
    blocked: bool = False
    claim_history: list[tuple[str, str]] = field(default_factory=list)
    progress_comments: list[str] = field(default_factory=list)
    pr_link_comments: list[str] = field(default_factory=list)
    linked_prs: list[PullRequest] = field(default_factory=list)


class FakeGitHubTracker:
    """In-memory tracker for orchestrator tests.

    Tests construct one with a list of issues, optionally flip eligibility
    via :meth:`set_issue_state` / :meth:`set_issue_labels`, and inspect
    :attr:`states` to assert claim / release ordering. Thread-safe under
    the orchestrator's serial poll loop; the lock is defensive in case a
    test spawns concurrent workers.
    """

    def __init__(
        self,
        issues: list[Issue] | None = None,
        *,
        ready_label: str = "symphony-ready",
        claim_label: str = "symphony-running",
    ) -> None:
        self._lock = threading.Lock()
        self.ready_label = ready_label
        self.claim_label = claim_label
        self.states: dict[str, _IssueState] = {
            i.identifier: _IssueState(issue=i) for i in (issues or [])
        }
        # Scripted failure hooks; default no-op. Each is a callable
        # invoked with the tracker method's positional args; raise to
        # simulate a transport error.
        self.claim_failure: callable | None = None
        self.release_failure: callable | None = None

    # -- Mutation helpers used by tests --------------------------------------

    def add_issue(self, issue: Issue) -> None:
        with self._lock:
            self.states[issue.identifier] = _IssueState(issue=issue)

    def set_issue_state(self, identifier: str, state: str) -> None:
        with self._lock:
            st = self.states[identifier]
            st.issue = replace(st.issue, state=state)

    def set_issue_labels(self, identifier: str, labels: tuple[str, ...]) -> None:
        with self._lock:
            st = self.states[identifier]
            st.issue = replace(st.issue, labels=labels)

    def add_linked_pr(self, identifier: str, pr: PullRequest) -> None:
        with self._lock:
            self.states[identifier].linked_prs.append(pr)

    # -- Protocol surface ----------------------------------------------------

    def fetch_candidate_issues(self) -> list[Issue]:
        with self._lock:
            return [
                st.issue
                for st in self.states.values()
                if st.issue.state == "open"
                and not st.blocked
                and st.claimed_by is None
                and (
                    self.ready_label in st.issue.labels
                    or self.ready_label == ""  # tracker not configured to require label
                )
            ]

    def fetch_issues_by_numbers(self, numbers: list[int]) -> list[Issue]:
        wanted = set(numbers)
        with self._lock:
            return [st.issue for st in self.states.values() if st.issue.number in wanted]

    def claim_issue(self, issue: Issue, run_metadata: dict[str, Any]) -> ClaimResult:
        if self.claim_failure is not None:
            raise TrackerError(self.claim_failure(issue, run_metadata) or "claim failed")
        with self._lock:
            st = self.states.get(issue.identifier)
            if st is None:
                return ClaimResult(ok=False, reason="unknown issue")
            if st.claimed_by is not None and st.claimed_by != run_metadata.get("run_id"):
                return ClaimResult(ok=False, conflict=True, reason="already claimed")
            st.claimed_by = run_metadata.get("run_id", "anonymous")
            st.claim_history.append((_now_iso(), f"claim:{st.claimed_by}"))
            # Add the claim label to the issue snapshot too, since real
            # GitHub would.
            existing = list(st.issue.labels)
            if self.claim_label not in existing:
                existing.append(self.claim_label)
                st.issue = replace(st.issue, labels=tuple(existing))
            return ClaimResult(ok=True)

    def release_issue(self, issue: Issue, reason: str) -> ReleaseResult:
        if self.release_failure is not None:
            raise TrackerError(self.release_failure(issue, reason) or "release failed")
        with self._lock:
            st = self.states.get(issue.identifier)
            if st is None:
                return ReleaseResult(ok=False, reason="unknown issue")
            st.claimed_by = None
            st.claim_history.append((_now_iso(), f"release:{reason}"))
            existing = [lbl for lbl in st.issue.labels if lbl != self.claim_label]
            st.issue = replace(st.issue, labels=tuple(existing))
            return ReleaseResult(ok=True)

    def mark_issue_blocked(self, issue: Issue, reason: str) -> ReleaseResult:
        with self._lock:
            st = self.states.get(issue.identifier)
            if st is None:
                return ReleaseResult(ok=False, reason="unknown issue")
            st.blocked = True
            st.claim_history.append((_now_iso(), f"blocked:{reason}"))
            return ReleaseResult(ok=True)

    def find_linked_pull_requests(self, issue: Issue) -> list[PullRequest]:
        with self._lock:
            st = self.states.get(issue.identifier)
            return list(st.linked_prs) if st else []

    def create_or_update_progress_comment(self, issue: Issue, body: str) -> None:
        with self._lock:
            self.states[issue.identifier].progress_comments.append(body)

    def create_or_update_pr_link_comment(self, issue: Issue, pr: PullRequest) -> None:
        with self._lock:
            self.states[issue.identifier].pr_link_comments.append(pr.url)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
