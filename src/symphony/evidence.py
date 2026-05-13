"""Task-outcome evidence detector (#60, M5.2).

Implements the task-outcome detection contract defined in
``SPEC.md`` §17.1–§17.7 and ``docs/terminal-outcomes.md``. The
orchestrator's worker finally-block calls :meth:`EvidenceDetector.detect`
just before writing ``terminal.json``; the result populates the new
``task_outcome``, ``task_evidence``, ``no_pr_reason``, and
``outcome_decided_by`` fields.

Evidence sources (in priority order):

1. **Linked PR** — via :func:`symphony.github.pr.find_linked_pull_requests`
   on the tracker's GitHubClient. A PR matching the expected branch
   name is sufficient evidence for ``completed_with_pr``.
2. **Role outcome sentinel** — a documented marker in the last assistant
   message text or terminal payload. Role workflows use
   ``Symphony-Role-Outcome: <transition>`` to request one of the
   current role's allowed graph transitions without letting the agent
   mutate tracker labels directly.
3. **No-PR sentinel** — a documented marker in the last assistant
   message text or a recent issue comment. SPEC §17.5 reserves the
   sentinel format ``Symphony-No-PR: <reason>``.
4. **Permission denials** — from ``permission_denials_count`` already
   on the worker. When non-zero AND no PR / no sentinel, promotes a
   COMPLETED run to ``incomplete_permission_denied``.
5. **Pushed branch** — best-effort ``git ls-remote`` (skipped in tests
   that don't wire a real workspace) to surface a branch the agent
   pushed without opening a PR. Necessary-but-not-sufficient per
   SPEC §17.3.
6. **Local diff in workspace** — ``git status --porcelain`` and
   ``git log origin/<base>..HEAD`` to detect uncommitted edits or
   local commits that didn't reach GitHub. Informational only — does
   NOT promote ``incomplete_no_evidence`` to ``completed_*``.

When the provider terminal_state is NOT ``completed``, the detector
short-circuits to the derivation rules in SPEC §17.4
(``outcome_decided_by="derivation"``); evidence collection is skipped
because the misleading-success class only applies to clean provider
completions.

The detector takes a tracker handle, a GitHubConfig, and a
:class:`GitHubClient` (for the read-side PR lookup). Tests inject a
:class:`FakeGitHubTracker` plus a tiny stub client; the production CLI
wires the real tracker's authenticated client.
"""

from __future__ import annotations

import logging
import re
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from symphony.config import GitHubConfig
from symphony.events import AgentEvent
from symphony.github.client import GitHubClient, GitHubError
from symphony.github.pr import expected_branch_name, find_linked_pull_requests
from symphony.models import Issue, PullRequest
from symphony.provider.base import Terminal

_LOG = logging.getLogger("symphony.evidence")

# -- task_outcome enum (mirrors SPEC §17.2 — string constants kept here so
#    the orchestrator and tests don't reach into a private symbol table) ---

OUTCOME_COMPLETED_WITH_PR = "completed_with_pr"
OUTCOME_COMPLETED_ROLE_OUTCOME = "completed_role_outcome"
OUTCOME_COMPLETED_NO_PR_DECLARED = "completed_no_pr_declared"
OUTCOME_INCOMPLETE_NO_EVIDENCE = "incomplete_no_evidence"
OUTCOME_INCOMPLETE_PERMISSION_DENIED = "incomplete_permission_denied"
OUTCOME_BLOCKED_OPERATOR_REQUIRED = "blocked_operator_required"
OUTCOME_RETRYABLE_FAILURE = "retryable_failure"
OUTCOME_UNKNOWN = "unknown"

ALL_OUTCOMES: frozenset[str] = frozenset(
    {
        OUTCOME_COMPLETED_WITH_PR,
        OUTCOME_COMPLETED_ROLE_OUTCOME,
        OUTCOME_COMPLETED_NO_PR_DECLARED,
        OUTCOME_INCOMPLETE_NO_EVIDENCE,
        OUTCOME_INCOMPLETE_PERMISSION_DENIED,
        OUTCOME_BLOCKED_OPERATOR_REQUIRED,
        OUTCOME_RETRYABLE_FAILURE,
        OUTCOME_UNKNOWN,
    }
)

DECIDED_BY_DETECTOR = "detector"
DECIDED_BY_DERIVATION = "derivation"
DECIDED_BY_UNKNOWN = "unknown"

# SPEC §17.5: reserved sentinel format. Captures everything after the
# colon as the no-PR reason.
NO_PR_SENTINEL = re.compile(
    r"Symphony-No-PR\s*:\s*(?P<reason>.+?)(?:\n|$)",
    re.IGNORECASE,
)

# Role workflows reserve this marker for agent-owned role decisions.
# The value is a role-graph transition name, for example
# ``approved``, ``changes_requested``, or ``decision_to_impl``. The
# orchestrator validates the transition against the active role/state;
# agents must not apply labels themselves.
ROLE_OUTCOME_SENTINEL = re.compile(
    r"Symphony-Role-Outcome\s*:\s*(?P<transition>[A-Za-z0-9_-]+)(?:\n|$)",
    re.IGNORECASE,
)

REVIEW_APPROVAL_SENTINEL = re.compile(
    r"Symphony-Review-Approval\s*:\s*(?P<decision>approved|approve|yes)(?:\n|$)",
    re.IGNORECASE,
)

REVIEW_CHECKLIST_SENTINEL = re.compile(
    r"Symphony-Review-Checklist\s*:\s*(?P<status>pass|passed|fail|failed)",
    re.IGNORECASE,
)

DESIGN_CHECKLIST_SENTINEL = re.compile(
    r"Symphony-Design-Checklist\s*:\s*(?P<status>pass|passed|fail|failed)",
    re.IGNORECASE,
)

REVIEW_APPROVAL_LABEL = "symphony-review-approved"

# Bound the assistant-text scan so a runaway transcript can't pin the
# detector reading megabytes of history.
_MAX_ASSISTANT_TEXT_SCAN_CHARS = 64_000

# Subprocess timeout for the workspace git probes. Long enough for a
# slow filesystem; short enough that a hung clone doesn't stall a
# poll tick.
_GIT_PROBE_TIMEOUT_S = 30.0


# -- Result types ------------------------------------------------------------


@dataclass(slots=True)
class DetectorResult:
    """Outcome of one evidence detection pass.

    Mirrors the SPEC §17.1 task-outcome row that lands in
    ``terminal.json``. The orchestrator does not interpret these
    fields beyond serialization (per leader scope on #60: no
    retry/block routing changes — that's #62).
    """

    task_outcome: str
    task_evidence: list[dict[str, Any]] = field(default_factory=list)
    role_outcome: str | None = None
    no_pr_reason: str | None = None
    outcome_decided_by: str = DECIDED_BY_DETECTOR
    task_outcome_recorded_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    def to_terminal_fields(self) -> dict[str, Any]:
        """Render as the new SPEC §17.1 fields for ``terminal.json``."""
        return {
            "task_outcome": self.task_outcome,
            "task_evidence": list(self.task_evidence),
            "role_outcome": self.role_outcome,
            "no_pr_reason": self.no_pr_reason,
            "outcome_decided_by": self.outcome_decided_by,
            "task_outcome_recorded_at": self.task_outcome_recorded_at.isoformat(),
        }


# -- Detector ----------------------------------------------------------------


class EvidenceDetector:
    """Per-run evidence collector.

    Construction takes the tracker-side handles needed for read-side
    queries; one instance per orchestrator is fine (the orchestrator
    builds it from ``tracker.client`` + ``config.github`` at startup).

    ``client`` may be ``None`` for tests that don't exercise the PR
    lookup; in that case the PR-detection step short-circuits with no
    evidence rather than raising.
    """

    def __init__(
        self,
        github: GitHubConfig,
        *,
        client: GitHubClient | None = None,
        git_probe_timeout: float = _GIT_PROBE_TIMEOUT_S,
    ) -> None:
        self._github = github
        self._client = client
        self._git_probe_timeout = git_probe_timeout

    def detect(
        self,
        *,
        issue: Issue,
        terminal_state: Terminal | None,
        retryable: bool,
        blocked: bool,
        permission_denials_count: int,
        last_event: AgentEvent | None,
        recent_assistant_text: str,
        workspace_path: Path | None,
        session_started_at: datetime | None = None,
    ) -> DetectorResult:
        """Classify the worker's task outcome.

        Branches:

        - Provider was NOT cleanly completed → SPEC §17.4 derivation
          (no detector traffic, no GitHub call). ``outcome_decided_by =
          "derivation"``.
        - Provider COMPLETED → run the full evidence pipeline below.

        The pipeline collects evidence in priority order (PR → no-PR
        sentinel → permission denials → branch → diff) and decides
        ``task_outcome`` from the strongest signal found. All evidence
        entries are kept in ``task_evidence`` regardless of which one
        decides — operators auditing the run can see the full picture.
        """
        if terminal_state != Terminal.COMPLETED:
            return self._derive(
                terminal_state=terminal_state,
                retryable=retryable,
                blocked=blocked,
                permission_denials_count=permission_denials_count,
            )

        evidence: list[dict[str, Any]] = []
        # 1. Linked PR — strongest signal.
        prs = self._detect_pull_requests(issue)
        # ``prs is None`` means we could not query GitHub at all (no client
        # wired). That is distinct from "queried, found nothing" (empty
        # list) — we MUST NOT downgrade an unverifiable run to
        # ``incomplete_no_evidence`` because routing in #62 treats that
        # value as operator-must-intervene. Tracked separately so the
        # final decision below can return ``unknown`` rather than block.
        pr_query_ran = prs is not None
        for pr in prs or []:
            evidence.append(
                {
                    "type": "pr_linked",
                    "url": pr.url,
                    "number": pr.number,
                    "state": pr.state,
                    "head_ref": pr.head_ref,
                    # We can't tell from a single read whether THIS run
                    # created the PR vs. updated it; conservative
                    # default is False (the run linked an existing PR).
                    # M5.3 may tighten this by comparing against the
                    # prior tick's PR snapshot.
                    "created": False,
                }
            )
        if prs:
            evidence.extend(self._detect_pull_request_review_gates(issue, prs))

        # 2. Role outcome sentinel (assistant text + last event payload).
        role_outcome = self._detect_role_outcome_sentinel(last_event, recent_assistant_text)
        if role_outcome is not None:
            evidence.append(
                {
                    "type": "role_outcome",
                    "transition": role_outcome,
                    "marker_source": "assistant_message",
                }
            )
        design_checklist = _design_checklist_evidence_from_texts(
            self._sentinel_haystacks(last_event, recent_assistant_text)
        )
        if role_outcome in {"decision_to_impl", "decision_to_review"}:
            design_checklist.extend(
                self._detect_design_checklist_comments(
                    issue,
                    session_started_at=session_started_at,
                )
            )
        evidence.extend(design_checklist)

        # 3. No-PR sentinel (assistant text + last event payload).
        sentinel_reason = self._detect_no_pr_sentinel(last_event, recent_assistant_text)
        if sentinel_reason is not None:
            evidence.append(
                {
                    "type": "no_pr_declared",
                    "reason": sentinel_reason,
                    "marker_source": "assistant_message",
                }
            )

        # 4. Permission denials — recorded for completeness even when a
        #    PR was found (operator may want to know Claude was bounced
        #    on something during the run).
        if permission_denials_count > 0:
            evidence.append(
                {
                    "type": "permission_denied",
                    "denials_count": permission_denials_count,
                    "tool_names": _extract_denied_tools(last_event),
                }
            )

        # 5. Pushed branch — best-effort. Skipped when no workspace path.
        branch = self._detect_pushed_branch(issue, workspace_path)
        if branch is not None:
            evidence.append(
                {
                    "type": "branch_pushed",
                    "name": branch["name"],
                    "head_sha": branch["head_sha"],
                }
            )

        # 6. Local diff — best-effort. Informational; not a promoter.
        diff = self._detect_local_diff(workspace_path)
        if diff is not None:
            evidence.append(
                {
                    "type": "diff_in_workspace",
                    "files_changed": diff["files_changed"],
                    "lines_added": diff["lines_added"],
                    "lines_removed": diff["lines_removed"],
                }
            )

        # Decision tree: role outcome wins for role workflows because a
        # reviewer or leader may inspect an existing PR without creating
        # a new one. The orchestrator still verifies the requested
        # transition against the active role and required evidence.
        if role_outcome is not None:
            return DetectorResult(
                task_outcome=OUTCOME_COMPLETED_ROLE_OUTCOME,
                task_evidence=evidence,
                role_outcome=role_outcome,
                outcome_decided_by=DECIDED_BY_DETECTOR,
            )
        if prs:
            return DetectorResult(
                task_outcome=OUTCOME_COMPLETED_WITH_PR,
                task_evidence=evidence,
                outcome_decided_by=DECIDED_BY_DETECTOR,
            )
        if sentinel_reason is not None:
            return DetectorResult(
                task_outcome=OUTCOME_COMPLETED_NO_PR_DECLARED,
                task_evidence=evidence,
                role_outcome=role_outcome,
                no_pr_reason=sentinel_reason,
                outcome_decided_by=DECIDED_BY_DETECTOR,
            )
        if permission_denials_count > 0:
            return DetectorResult(
                task_outcome=OUTCOME_INCOMPLETE_PERMISSION_DENIED,
                task_evidence=evidence,
                outcome_decided_by=DECIDED_BY_DETECTOR,
            )
        # No PR (queried), no declaration, no denials → real
        # misleading-success. #62 routes this to mark_issue_blocked.
        if pr_query_ran:
            return DetectorResult(
                task_outcome=OUTCOME_INCOMPLETE_NO_EVIDENCE,
                task_evidence=evidence,
                outcome_decided_by=DECIDED_BY_DETECTOR,
            )
        # No PR query happened (no GitHubClient wired). We cannot
        # confidently call this misleading-success — return ``unknown``
        # so #62 routing does NOT mark the issue blocked. Tests using
        # FakeGitHubTracker (which has no `.client`) take this path.
        return DetectorResult(
            task_outcome=OUTCOME_UNKNOWN,
            task_evidence=evidence,
            outcome_decided_by=DECIDED_BY_DERIVATION,
        )

    # -- Internals -----------------------------------------------------------

    def _derive(
        self,
        *,
        terminal_state: Terminal | None,
        retryable: bool,
        blocked: bool,
        permission_denials_count: int,
    ) -> DetectorResult:
        """SPEC §17.4 derivation rules for non-COMPLETED provider runs.

        Permission-denied promotion still applies if a future code
        path reaches us with terminal_state==COMPLETED but bypasses
        the detector pipeline (defensive).
        """
        if terminal_state == Terminal.COMPLETED and permission_denials_count > 0:
            outcome = OUTCOME_INCOMPLETE_PERMISSION_DENIED
        elif terminal_state == Terminal.FAILED and blocked:
            outcome = OUTCOME_BLOCKED_OPERATOR_REQUIRED
        elif (
            terminal_state in {Terminal.FAILED, Terminal.CANCELLED, Terminal.CRASHED}
            and retryable
        ):
            outcome = OUTCOME_RETRYABLE_FAILURE
        elif terminal_state == Terminal.COMPLETED:
            outcome = OUTCOME_INCOMPLETE_NO_EVIDENCE
        else:
            outcome = OUTCOME_UNKNOWN
        return DetectorResult(
            task_outcome=outcome,
            task_evidence=[],
            outcome_decided_by=DECIDED_BY_DERIVATION,
        )

    def _detect_pull_requests(self, issue: Issue) -> list[PullRequest] | None:
        """Read-side PR lookup.

        Returns:
        - A list of PRs (possibly empty) when the lookup completed
          successfully — empty means "queried, found no PR".
        - ``None`` when we could NOT verify PR absence — either no
          GitHubClient is wired, OR the lookup raised a GitHubError
          (transient 500 / rate limit / network blip / auth issue).

        The ``None`` vs ``[]`` distinction is load-bearing for #62
        routing: ``[]`` causes ``incomplete_no_evidence`` →
        ``mark_issue_blocked`` (we verified no PR exists), while
        ``None`` keeps the outcome at ``unknown`` (we couldn't verify
        — don't escalate). A transient GitHub failure during a
        completed run MUST NOT cause Symphony to mark the issue
        blocked; the operator would then have to clear a label that
        was applied because of an API blip rather than a real
        misleading-success run.

        Maintainer note: do NOT change the GitHubError path to
        return ``[]``. The empty-list semantic is reserved for
        verified-no-PR.
        """
        if self._client is None:
            return None
        try:
            return find_linked_pull_requests(self._client, issue, self._github)
        except GitHubError as exc:
            # Detector failures should NOT crash the worker finally
            # block AND must NOT be conflated with verified absence
            # of a PR (#62 leader correction on PR #73). Returning
            # None routes the outcome to ``unknown`` instead of
            # ``incomplete_no_evidence``, so a transient GitHub
            # failure does not falsely block the issue.
            _LOG.warning(
                "evidence detector: PR lookup failed for %s: %s — "
                "outcome will be `unknown` (cannot verify PR absence).",
                issue.identifier,
                exc,
            )
            return None

    def _detect_pull_request_review_gates(
        self,
        issue: Issue,
        prs: list[PullRequest],
    ) -> list[dict[str, Any]]:
        if self._client is None:
            return []
        evidence: list[dict[str, Any]] = []
        for pr in prs:
            evidence.extend(self._detect_one_pull_request_review_gate(issue, pr))
        return evidence

    def _detect_one_pull_request_review_gate(
        self,
        issue: Issue,
        pr: PullRequest,
    ) -> list[dict[str, Any]]:
        try:
            reviews = self._client.get(
                f"/repos/{issue.owner}/{issue.repo}/pulls/{pr.number}/reviews"
            )
            review_threads = self._query_review_threads(issue, pr.number)
        except GitHubError as exc:
            _LOG.warning(
                "evidence detector: review gate lookup failed for %s PR #%s: %s",
                issue.identifier,
                pr.number,
                exc,
            )
            return [
                {
                    "type": "pr_review_gate_query_failed",
                    "number": pr.number,
                    "error": str(exc),
                }
            ]

        review_entry = _review_state_evidence(pr, reviews if isinstance(reviews, list) else [])
        threads_entry = _review_threads_evidence(pr.number, review_threads)
        review_comments = self._query_issue_and_pr_comments(issue, pr)
        approval_entries = self._detect_review_approval_overrides(issue, pr, review_comments)
        checklist_entries = _review_checklist_evidence(pr.number, review_comments)
        return [review_entry, threads_entry, *approval_entries, *checklist_entries]

    def _query_review_threads(self, issue: Issue, pr_number: int) -> list[dict[str, Any]]:
        query = """
        query($owner:String!, $repo:String!, $number:Int!) {
          repository(owner:$owner, name:$repo) {
            pullRequest(number:$number) {
              reviewThreads(first:100) {
                nodes {
                  isResolved
                  isOutdated
                  path
                  line
                  comments(first:20) {
                    nodes {
                      url
                      author { login }
                      body
                    }
                  }
                }
              }
            }
          }
        }
        """
        raw = self._client.post(
            "/graphql",
            json_body={
                "query": query,
                "variables": {
                    "owner": issue.owner,
                    "repo": issue.repo,
                    "number": pr_number,
                },
            },
        )
        if not isinstance(raw, dict):
            return []
        if raw.get("errors"):
            raise GitHubError(f"GraphQL reviewThreads returned errors: {raw['errors']!r}")
        nodes = (
            raw.get("data", {})
            .get("repository", {})
            .get("pullRequest", {})
            .get("reviewThreads", {})
            .get("nodes", [])
        )
        return nodes if isinstance(nodes, list) else []

    def _detect_review_approval_overrides(
        self,
        issue: Issue,
        pr: PullRequest,
        comments: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        evidence: list[dict[str, Any]] = []
        labels = {label.strip().lower() for label in issue.labels}
        if REVIEW_APPROVAL_LABEL in labels:
            evidence.append(
                {
                    "type": "review_approval_label",
                    "label": REVIEW_APPROVAL_LABEL,
                    "number": pr.number,
                }
            )
        evidence.extend(_review_approval_comment_evidence(pr.number, comments))
        return evidence

    def _query_issue_and_pr_comments(
        self,
        issue: Issue,
        pr: PullRequest,
    ) -> list[dict[str, Any]]:
        comments_out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for number, surface in ((issue.number, "issue"), (pr.number, "pull_request")):
            try:
                comments = self._client.get(
                    f"/repos/{issue.owner}/{issue.repo}/issues/{number}/comments"
                )
            except GitHubError as exc:
                _LOG.warning(
                    "evidence detector: review approval comment lookup failed for "
                    "%s %s #%s: %s",
                    issue.identifier,
                    surface,
                    number,
                    exc,
                )
                continue
            if not isinstance(comments, list):
                continue
            for comment in comments:
                url = str(comment.get("html_url") or comment.get("url") or "")
                dedupe_key = url or f"{surface}:{comment.get('id')}"
                if dedupe_key in seen:
                    continue
                seen.add(dedupe_key)
                item = dict(comment)
                item["_symphony_surface"] = surface
                item["_symphony_url"] = url or None
                comments_out.append(item)
        return comments_out

    def _query_issue_comments(self, issue: Issue) -> list[dict[str, Any]]:
        if self._client is None:
            return []
        try:
            comments = self._client.get(
                f"/repos/{issue.owner}/{issue.repo}/issues/{issue.number}/comments"
            )
        except GitHubError as exc:
            _LOG.warning(
                "evidence detector: issue comment lookup failed for %s: %s",
                issue.identifier,
                exc,
            )
            return []
        if not isinstance(comments, list):
            return []
        out: list[dict[str, Any]] = []
        for comment in comments:
            item = dict(comment)
            url = str(comment.get("html_url") or comment.get("url") or "")
            item["_symphony_surface"] = "issue"
            item["_symphony_url"] = url or None
            out.append(item)
        return out

    def _detect_design_checklist_comments(
        self,
        issue: Issue,
        *,
        session_started_at: datetime | None,
    ) -> list[dict[str, Any]]:
        comments = self._query_issue_comments(issue)
        if session_started_at is not None:
            comments = [
                comment
                for comment in comments
                if _comment_created_at_or_min(comment) >= session_started_at
            ]
        latest: list[dict[str, Any]] = []
        for comment in sorted(comments, key=_comment_created_at_or_min):
            evidence = _design_checklist_evidence_from_comments([comment])
            if evidence:
                latest = evidence
        return latest

    def _detect_no_pr_sentinel(
        self,
        last_event: AgentEvent | None,
        recent_assistant_text: str,
    ) -> str | None:
        """Find ``Symphony-No-PR: <reason>`` in assistant text or the last event payload."""
        for text in self._sentinel_haystacks(last_event, recent_assistant_text):
            match = NO_PR_SENTINEL.search(text)
            if match:
                return match.group("reason").strip()
        return None

    def _sentinel_haystacks(
        self,
        last_event: AgentEvent | None,
        recent_assistant_text: str,
    ) -> list[str]:
        haystacks: list[str] = []
        if recent_assistant_text:
            haystacks.append(recent_assistant_text[:_MAX_ASSISTANT_TEXT_SCAN_CHARS])
        if last_event is not None:
            payload = last_event.payload or {}
            for key in ("result", "text"):
                value = payload.get(key)
                if isinstance(value, str) and value:
                    haystacks.append(value[:_MAX_ASSISTANT_TEXT_SCAN_CHARS])
        return haystacks

    def _detect_role_outcome_sentinel(
        self,
        last_event: AgentEvent | None,
        recent_assistant_text: str,
    ) -> str | None:
        """Find ``Symphony-Role-Outcome: <transition>`` in assistant text."""
        for text in self._sentinel_haystacks(last_event, recent_assistant_text):
            match = ROLE_OUTCOME_SENTINEL.search(text)
            if match:
                return match.group("transition").strip()
        return None

    def _detect_pushed_branch(
        self, issue: Issue, workspace_path: Path | None
    ) -> dict[str, Any] | None:
        """Probe ``git ls-remote`` for the expected branch name.

        Returns the SHA the remote is currently at, or None when the
        remote doesn't have the branch / git isn't available / no
        workspace was supplied. Read-only probe — never mutates state.
        """
        if workspace_path is None or not (workspace_path / ".git").is_dir():
            return None
        branch = expected_branch_name(self._github, issue)
        completed = self._run_git(
            workspace_path,
            ["ls-remote", "--heads", "origin", branch],
        )
        if completed is None or completed.returncode != 0:
            return None
        line = (completed.stdout or "").strip().splitlines()[0:1]
        if not line:
            return None
        sha = line[0].split(maxsplit=1)[0].strip()
        if not sha:
            return None
        return {"name": branch, "head_sha": sha}

    def _detect_local_diff(self, workspace_path: Path | None) -> dict[str, Any] | None:
        """Best-effort `git status --porcelain` + base-branch ahead count.

        Returns None when no workspace, no .git, or git fails. Returns
        a zero-counts dict when the working tree is clean (informational
        — operator sees "no local edits" rather than missing field).
        """
        if workspace_path is None or not (workspace_path / ".git").is_dir():
            return None

        # Working-tree changes (uncommitted edits, untracked files).
        status = self._run_git(workspace_path, ["status", "--porcelain"])
        files_changed = 0
        if status is not None and status.returncode == 0:
            files_changed = sum(
                1 for line in (status.stdout or "").splitlines() if line.strip()
            )

        # Local commits ahead of base branch + line counts.
        base = self._github.base_branch
        # numstat output: "<added>\t<removed>\t<path>" per file.
        numstat = self._run_git(
            workspace_path,
            ["diff", "--numstat", f"origin/{base}..HEAD"],
        )
        lines_added = 0
        lines_removed = 0
        if numstat is not None and numstat.returncode == 0:
            for line in (numstat.stdout or "").splitlines():
                parts = line.split("\t", 2)
                if len(parts) < 3:
                    continue
                # Binary files have "-" in the count slots.
                added_str, removed_str, _path = parts
                lines_added += _safe_int(added_str)
                lines_removed += _safe_int(removed_str)
                files_changed += 1  # commit-only files also count as changes

        if files_changed == 0 and lines_added == 0 and lines_removed == 0:
            return None
        return {
            "files_changed": files_changed,
            "lines_added": lines_added,
            "lines_removed": lines_removed,
        }

    def _run_git(
        self, cwd: Path, argv: list[str]
    ) -> subprocess.CompletedProcess[str] | None:
        """Run ``git`` read-only. Returns None on OSError/timeout."""
        try:
            return subprocess.run(  # noqa: S603 — argv list, no shell
                ["git", *argv],
                cwd=str(cwd),
                capture_output=True,
                text=True,
                timeout=self._git_probe_timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            _LOG.debug("evidence detector git %s failed: %s", argv[0], exc)
            return None


# -- Helpers -----------------------------------------------------------------


def _extract_denied_tools(event: AgentEvent | None) -> list[str]:
    """Pull tool names from the SDK's ``permission_denials`` list."""
    if event is None:
        return []
    payload = event.payload or {}
    raw = payload.get("permission_denials")
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for entry in raw:
        if isinstance(entry, dict):
            name = entry.get("tool_name") or entry.get("name")
            if isinstance(name, str) and name:
                out.append(name)
    return out


def _safe_int(value: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _review_state_evidence(pr: PullRequest, reviews: list[dict[str, Any]]) -> dict[str, Any]:
    pr_author = _pr_author(pr)
    latest_by_author: dict[str, dict[str, Any]] = {}
    for review in reviews:
        author = ((review.get("user") or {}).get("login") or "").strip()
        if not author:
            continue
        submitted_at = str(review.get("submitted_at") or "")
        previous = latest_by_author.get(author)
        if previous is None or submitted_at >= str(previous.get("submitted_at") or ""):
            latest_by_author[author] = review

    approved_by: list[str] = []
    changes_requested_by: list[str] = []
    commented_by: list[str] = []
    for author, review in latest_by_author.items():
        state = str(review.get("state") or "").upper()
        if state == "APPROVED":
            approved_by.append(author)
        elif state == "CHANGES_REQUESTED":
            changes_requested_by.append(author)
        elif state == "COMMENTED":
            commented_by.append(author)

    independent_approved_by = sorted(author for author in approved_by if author != pr_author)
    return {
        "type": "pr_review_state",
        "number": pr.number,
        "url": pr.url,
        "pr_author": pr_author,
        "approved_by": sorted(approved_by),
        "independent_approved_by": independent_approved_by,
        "changes_requested_by": sorted(changes_requested_by),
        "commented_by": sorted(commented_by),
        "has_independent_approval": bool(independent_approved_by),
    }


def _review_threads_evidence(
    pr_number: int,
    threads: list[dict[str, Any]],
) -> dict[str, Any]:
    unresolved_unaddressed: list[dict[str, Any]] = []
    unresolved_addressed = 0
    unresolved_outdated = 0
    for thread in threads:
        if bool(thread.get("isResolved")):
            continue
        if bool(thread.get("isOutdated")):
            unresolved_outdated += 1
            continue
        comments = (
            ((thread.get("comments") or {}).get("nodes") or [])
            if isinstance(thread.get("comments"), dict)
            else []
        )
        first = comments[0] if comments else {}
        first_author = ((first.get("author") or {}).get("login") if first else None)
        has_followup_reply = any(
            ((comment.get("author") or {}).get("login")) != first_author
            for comment in comments[1:]
        )
        if has_followup_reply:
            unresolved_addressed += 1
            continue
        unresolved_unaddressed.append(
            {
                "path": thread.get("path"),
                "line": thread.get("line"),
                "url": first.get("url"),
                "author": first_author,
                "body": _truncate(str(first.get("body") or ""), 240) if first else "",
            }
        )

    return {
        "type": "pr_review_threads",
        "number": pr_number,
        "unresolved_current_count": len(unresolved_unaddressed),
        "unresolved_unaddressed_count": len(unresolved_unaddressed),
        "unresolved_addressed_count": unresolved_addressed,
        "unresolved_outdated_count": unresolved_outdated,
        "unresolved_count": len(unresolved_unaddressed)
        + unresolved_addressed
        + unresolved_outdated,
        "examples": unresolved_unaddressed[:3],
    }


def _review_approval_comment_evidence(
    pr_number: int,
    comments: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    for comment in comments:
        body = str(comment.get("body") or "")
        if not REVIEW_APPROVAL_SENTINEL.search(body):
            continue
        evidence.append(
            {
                "type": "review_approval_comment",
                "number": pr_number,
                "surface": comment.get("_symphony_surface"),
                "url": comment.get("_symphony_url"),
                "author": ((comment.get("user") or {}).get("login")),
            }
        )
    return evidence


def _review_checklist_evidence(
    pr_number: int,
    comments: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    for comment in comments:
        body = str(comment.get("body") or "")
        match = REVIEW_CHECKLIST_SENTINEL.search(body)
        if not match:
            continue
        raw_status = match.group("status").lower()
        status = "pass" if raw_status in {"pass", "passed"} else "fail"
        evidence.append(
            {
                "type": "review_checklist",
                "number": pr_number,
                "status": status,
                "passed": status == "pass",
                "surface": comment.get("_symphony_surface"),
                "url": comment.get("_symphony_url"),
                "author": ((comment.get("user") or {}).get("login")),
                "has_spec_compliance": _has_checked_item(body, "spec_compliance"),
                "has_issue_fit": _has_checked_item(body, "issue_fit"),
                "has_existing_design_fit": _has_checked_item(body, "existing_design_fit"),
                "has_tests": _has_checked_item(body, "tests"),
                "has_review_threads": _has_checked_item(body, "review_threads"),
            }
        )
    return evidence


def _has_checked_item(body: str, key: str) -> bool:
    pattern = re.compile(
        rf"(?:^|\n)\s*-\s*\[[xX]\]\s*{re.escape(key)}\s*:",
        re.IGNORECASE,
    )
    return bool(pattern.search(body))


def _design_checklist_evidence_from_texts(texts: list[str]) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    for text in texts:
        match = DESIGN_CHECKLIST_SENTINEL.search(text)
        if not match:
            continue
        raw_status = match.group("status").lower()
        status = "pass" if raw_status in {"pass", "passed"} else "fail"
        evidence.append(
            {
                "type": "design_checklist",
                "status": status,
                "passed": status == "pass",
                "has_problem_framing": _has_checked_item(text, "problem_framing"),
                "has_existing_mechanism_fit": _has_checked_item(
                    text, "existing_mechanism_fit"
                ),
                "has_minimal_surface_area": _has_checked_item(text, "minimal_surface_area"),
                "has_data_model_fit": _has_checked_item(text, "data_model_fit"),
                "has_test_strategy": _has_checked_item(text, "test_strategy"),
                "has_drift_assessment": _has_checked_item(text, "drift_assessment"),
            }
        )
    return evidence


def _design_checklist_evidence_from_comments(
    comments: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    for comment in comments:
        body = str(comment.get("body") or "")
        for entry in _design_checklist_evidence_from_texts([body]):
            entry.update(
                {
                    "surface": comment.get("_symphony_surface"),
                    "url": comment.get("_symphony_url"),
                    "author": ((comment.get("user") or {}).get("login")),
                    "created_at": comment.get("created_at"),
                }
            )
            evidence.append(entry)
    return evidence


def _comment_created_at_or_min(comment: dict[str, Any]) -> datetime:
    raw = str(comment.get("created_at") or "")
    if not raw:
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)


def _pr_author(pr: PullRequest) -> str:
    raw = pr.raw if isinstance(pr.raw, dict) else {}
    user = raw.get("user") or {}
    login = user.get("login") if isinstance(user, dict) else None
    return str(login or "").strip()


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def collect_recent_assistant_text(events: Iterable[AgentEvent], *, limit: int = 16) -> str:
    """Concatenate the text of the **most recent** ``message_delta`` events.

    Returns up to ``limit`` matches, preserving chronological order
    (oldest-of-the-tail first). The detector's sentinel scan benefits
    from the most recent assistant text — Claude's final
    "Symphony-No-PR: …" declaration sits at the END of the transcript,
    not the start, so a buggy "first N" walk would miss it on any
    session longer than ``limit`` deltas.

    Implemented with a bounded :class:`collections.deque` so memory
    stays O(limit) regardless of transcript length — the orchestrator
    may pass a long event tail in long-running sessions and we MUST
    NOT pin the whole history just to scan the last few chunks.

    Bug history: the M5.2 (#60) version walked from the start and
    stopped at ``limit``, returning the OLDEST chunks — a sentinel in
    Claude's final message would slip past undetected on any
    session longer than 16 deltas. Fixed in #63 (M5.4) per leader
    note carried from PR #72 and #73.
    """
    from collections import deque

    chunks: deque[str] = deque(maxlen=limit)
    for event in events:
        if event.event != "message_delta":
            continue
        text = (event.payload or {}).get("text")
        if isinstance(text, str) and text:
            chunks.append(text)
    return "\n".join(chunks)


__all__ = [
    "ALL_OUTCOMES",
    "DECIDED_BY_DERIVATION",
    "DECIDED_BY_DETECTOR",
    "DECIDED_BY_UNKNOWN",
    "DetectorResult",
    "EvidenceDetector",
    "NO_PR_SENTINEL",
    "OUTCOME_BLOCKED_OPERATOR_REQUIRED",
    "OUTCOME_COMPLETED_ROLE_OUTCOME",
    "OUTCOME_COMPLETED_NO_PR_DECLARED",
    "OUTCOME_COMPLETED_WITH_PR",
    "OUTCOME_INCOMPLETE_NO_EVIDENCE",
    "OUTCOME_INCOMPLETE_PERMISSION_DENIED",
    "OUTCOME_RETRYABLE_FAILURE",
    "OUTCOME_UNKNOWN",
    "ROLE_OUTCOME_SENTINEL",
    "collect_recent_assistant_text",
]
