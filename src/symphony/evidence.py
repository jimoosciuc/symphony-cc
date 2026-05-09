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
2. **No-PR sentinel** — a documented marker in the last assistant
   message text or a recent issue comment. SPEC §17.5 reserves the
   sentinel format ``Symphony-No-PR: <reason>``.
3. **Permission denials** — from ``permission_denials_count`` already
   on the worker. When non-zero AND no PR / no sentinel, promotes a
   COMPLETED run to ``incomplete_permission_denied``.
4. **Pushed branch** — best-effort ``git ls-remote`` (skipped in tests
   that don't wire a real workspace) to surface a branch the agent
   pushed without opening a PR. Necessary-but-not-sufficient per
   SPEC §17.3.
5. **Local diff in workspace** — ``git status --porcelain`` and
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
OUTCOME_COMPLETED_NO_PR_DECLARED = "completed_no_pr_declared"
OUTCOME_INCOMPLETE_NO_EVIDENCE = "incomplete_no_evidence"
OUTCOME_INCOMPLETE_PERMISSION_DENIED = "incomplete_permission_denied"
OUTCOME_BLOCKED_OPERATOR_REQUIRED = "blocked_operator_required"
OUTCOME_RETRYABLE_FAILURE = "retryable_failure"
OUTCOME_UNKNOWN = "unknown"

ALL_OUTCOMES: frozenset[str] = frozenset(
    {
        OUTCOME_COMPLETED_WITH_PR,
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

        # 2. No-PR sentinel (assistant text + last event payload).
        sentinel_reason = self._detect_no_pr_sentinel(last_event, recent_assistant_text)
        if sentinel_reason is not None:
            evidence.append(
                {
                    "type": "no_pr_declared",
                    "reason": sentinel_reason,
                    "marker_source": "assistant_message",
                }
            )

        # 3. Permission denials — recorded for completeness even when a
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

        # 4. Pushed branch — best-effort. Skipped when no workspace path.
        branch = self._detect_pushed_branch(issue, workspace_path)
        if branch is not None:
            evidence.append(
                {
                    "type": "branch_pushed",
                    "name": branch["name"],
                    "head_sha": branch["head_sha"],
                }
            )

        # 5. Local diff — best-effort. Informational; not a promoter.
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

        # Decision tree: strongest evidence wins.
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

    def _detect_no_pr_sentinel(
        self,
        last_event: AgentEvent | None,
        recent_assistant_text: str,
    ) -> str | None:
        """Find ``Symphony-No-PR: <reason>`` in assistant text or the last event payload."""
        haystacks: list[str] = []
        if recent_assistant_text:
            haystacks.append(recent_assistant_text[:_MAX_ASSISTANT_TEXT_SCAN_CHARS])
        if last_event is not None:
            payload = last_event.payload or {}
            for key in ("result", "text"):
                value = payload.get(key)
                if isinstance(value, str) and value:
                    haystacks.append(value[:_MAX_ASSISTANT_TEXT_SCAN_CHARS])
        for text in haystacks:
            match = NO_PR_SENTINEL.search(text)
            if match:
                return match.group("reason").strip()
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
    "OUTCOME_COMPLETED_NO_PR_DECLARED",
    "OUTCOME_COMPLETED_WITH_PR",
    "OUTCOME_INCOMPLETE_NO_EVIDENCE",
    "OUTCOME_INCOMPLETE_PERMISSION_DENIED",
    "OUTCOME_RETRYABLE_FAILURE",
    "OUTCOME_UNKNOWN",
    "collect_recent_assistant_text",
]
