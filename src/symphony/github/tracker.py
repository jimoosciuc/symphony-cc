"""GitHub tracker boundary + an in-memory fake for tests.

The orchestrator depends on the :class:`TrackerProtocol`. The real
GitHub-API-backed adapter lands in #8 — until then, all orchestrator
behavior is exercised against :class:`FakeGitHubTracker`.

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

import threading
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Protocol

from symphony.models import Issue, PullRequest

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
            # Mirror the real GitHubTracker: marking an issue blocked
            # also drops the active claim so reconciliation no longer
            # treats it as owned by this run.
            st.claimed_by = None
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
