"""Symphony orchestrator core.

The orchestrator owns:

- Polling the tracker for candidate issues.
- Reconciling active workers when issue state changes (closed, label
  change, linked PR merged).
- Dispatching eligible issues up to ``agent.max_concurrency``.
- Running each worker through workspace prepare → claim → provider
  start_session → send_input loop → release.
- Recording every event to ``events.jsonl`` via :class:`ArtifactWriter`.
- Scheduling retries with exponential backoff per :class:`RetryConfig`.

The class is async and small on purpose: a single :meth:`run_once`
method advances the daemon by one tick. Tests drive it tick-by-tick;
:meth:`run_forever` is a thin loop around ``run_once`` + ``asyncio.sleep``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from symphony.artifact_retention import ArtifactRetentionExecutor
from symphony.artifacts import ArtifactWriter, redact_text
from symphony.cleanup import WorkspaceCleanupExecutor
from symphony.config import LaneConfig, WorkflowConfig
from symphony.events import TERMINAL_TURN_EVENTS, AgentEvent
from symphony.evidence import (
    OUTCOME_COMPLETED_NO_PR_DECLARED,
    OUTCOME_COMPLETED_WITH_PR,
    OUTCOME_INCOMPLETE_NO_EVIDENCE,
    OUTCOME_INCOMPLETE_PERMISSION_DENIED,
    DetectorResult,
    EvidenceDetector,
)
from symphony.github.tracker import ClaimResult, TrackerError, TrackerProtocol
from symphony.models import Issue, Workspace
from symphony.provider.base import (
    AgentProviderProtocol,
    ProviderError,
    ProviderRestoreError,
    ProviderRetryableError,
    SessionRecord,
    Terminal,
)
from symphony.recovery import (
    ACTION_BLOCKED,
    ACTION_DISCARDED,
    ACTION_RELEASED,
    ACTION_RESUMED,
    ACTION_SKIPPED,
    RecoveryDecision,
    discover_persisted_records,
    discover_workspace_records,
    issue_is_actionable,
)
from symphony.remote.dispatcher import RemoteIssueDispatcher
from symphony.remote.runner import RemoteDispatchRunResult
from symphony.retry import RetryState, next_backoff_ms
from symphony.status import build_status_snapshot
from symphony.usage import UsageTotals
from symphony.workflow import render_prompt
from symphony.workflow_reload import WorkflowReloader
from symphony.workspace import WorkspaceManager

_LOG = logging.getLogger("symphony.orchestrator")


# A continuation policy lets tests inject "should we run another turn?"
# without depending on a real prompt renderer. Returns the message to send
# next or None to terminate the worker. The default policy runs exactly
# one turn (returns the rendered first prompt once, then None).
ContinuationPolicy = Callable[["WorkerState"], str | None]


@dataclass(slots=True)
class WorkerState:
    """In-memory state for one active dispatched issue.

    Mutable so the orchestrator can update ``turn_count`` and
    ``terminal_state`` as the worker progresses without re-allocating the
    whole record.

    ``timeout_subtype`` is set by the orchestrator's stall/turn-timeout
    enforcement to ``"stall_timeout"`` or ``"turn_timeout"``; the
    finally-block uses it to write a richer ``terminal.json`` and to
    schedule a retry.
    """

    issue: Issue
    workspace: Workspace
    session: SessionRecord
    artifacts: ArtifactWriter
    config: WorkflowConfig | None = None
    turn_count: int = 0
    terminal_state: Terminal | None = None
    last_event: AgentEvent | None = None
    recent_events: list[AgentEvent] = field(default_factory=list)
    error: str | None = None
    timeout_subtype: str | None = None
    usage: UsageTotals = field(default_factory=UsageTotals)
    lane: LaneConfig | None = None


@dataclass(slots=True)
class TickResult:
    """What happened during one :meth:`Orchestrator.run_once` call.

    Returned for tests + ops dashboards. ``dispatched`` and ``finished``
    are issue identifiers; ``reconciled_cancelled`` lists workers whose
    issues fell out of eligibility during reconciliation.
    """

    dispatched: list[str] = field(default_factory=list)
    finished: list[str] = field(default_factory=list)
    reconciled_cancelled: list[str] = field(default_factory=list)
    skipped_claim_conflict: list[str] = field(default_factory=list)
    retries_scheduled: list[str] = field(default_factory=list)
    workflow_reloaded: bool = False
    workflow_reload_error: str | None = None
    dispatch_paused: bool = False


class Orchestrator:
    """Coordinates tracker, workspace manager, and provider per SPEC §14."""

    def __init__(
        self,
        config: WorkflowConfig,
        *,
        tracker: TrackerProtocol,
        provider: AgentProviderProtocol,
        workspace_manager: WorkspaceManager,
        continuation_policy: ContinuationPolicy | None = None,
        run_id: str | None = None,
        clock: Callable[[], datetime] | None = None,
        evidence_detector: EvidenceDetector | None = None,
        workflow_reloader: WorkflowReloader | None = None,
        remote_dispatcher: RemoteIssueDispatcher | None = None,
    ) -> None:
        self.config = config
        self.tracker = tracker
        self.provider = provider
        self.workspaces = workspace_manager
        self.run_id = run_id or f"run-{uuid.uuid4().hex[:8]}"
        self._clock = clock or _now_utc
        self._workflow_reloader = workflow_reloader or WorkflowReloader.from_config(
            config,
            clock=self._clock,
        )
        self.continuation_policy = continuation_policy or self._default_continuation_policy
        self.remote_dispatcher = remote_dispatcher

        # Mutable state.
        self.active: dict[str, WorkerState] = {}
        self.retry_states: dict[str, RetryState] = {}
        self.recent_finished: list[dict[str, Any]] = []
        # Recovery handoff: when restart recovery decides to drop a session
        # and let normal dispatch start a fresh one (the
        # ``new_session_with_summary`` policy), it stashes the prior
        # provider session ids here. ``_start_worker`` consumes the entry
        # on next claim so the new ``SessionRecord.previous_provider_session_ids``
        # carries the chain — the continuation prompt can then reference
        # the prior conversation.
        self._restart_carryover: dict[str, list[str]] = {}
        # Decisions made by the most recent ``recover()`` call. Surfaced
        # for tests + the CLI's startup summary.
        self.recovery_decisions: list[RecoveryDecision] = []
        # M5.2 #60 evidence detector. Optional injection so unit tests
        # can substitute a stub or pass ``None`` (the default detector
        # falls back to SPEC §17.4 derivation when no GitHubClient is
        # supplied — safe for tests using FakeGitHubTracker without a
        # real REST surface).
        self._evidence = evidence_detector or EvidenceDetector(
            self.config.github,
            client=getattr(tracker, "client", None),
        )
        # M5.7 #66 cleanup executor. Disabled by default
        # (workspace.cleanup.enabled=False keeps existing SPEC §8 reuse
        # semantics). When enabled, the worker finally block triggers
        # ``cleanup_for_terminal_issue`` and ``run_once`` triggers
        # ``sweep_for_age`` and ``_sweep_for_closed_prs`` (the latter
        # wires ``cleanup_for_closed_pr`` per #82).
        self._cleanup = WorkspaceCleanupExecutor(
            workspace_manager,
            self.config.workspace.cleanup,
        )
        self._artifact_retention = ArtifactRetentionExecutor(
            self.config.claude.artifact_store,
            self.config.claude.artifact_retention,
            redact_keys=self.config.logging.redact_keys,
            clock=self._clock,
        )

    # -- Public API ---------------------------------------------------------

    async def run_once(self) -> TickResult:
        result = TickResult()

        reload_result = self._maybe_reload_workflow()
        if reload_result is not None:
            result.workflow_reloaded = reload_result.reloaded
            result.workflow_reload_error = reload_result.error
            result.dispatch_paused = reload_result.dispatch_paused

        # 0. M5.7 #66: age-based workspace sweep BEFORE reconcile.
        # ``self.active`` here reflects the *previous* tick's workers
        # (reconcile hasn't run yet for this tick) — that's intentional:
        # the workers carried over from last tick are exactly the ones
        # currently holding workspaces on disk. Reconcile may extend
        # ``self.active`` further down, but those new workers will create
        # fresh workspaces (mtime=now) which can't be too-old anyway.
        # No-op when workspace.cleanup.enabled is False or max_age_days
        # is unset. Each decision is logged by the executor; we don't
        # surface workspace decisions in TickResult; artifact retention
        # writes its own operator report under claude.artifact_store.
        self._artifact_retention.sweep()
        self._cleanup.sweep_for_age(active_identifiers=set(self.active.keys()))

        # 0.5. M5.7 #66 / #82: closed/merged-PR workspace sweep, also
        # before reconcile and using the same ``self.active`` snapshot
        # for the same reason. No-op when workspace.cleanup.enabled or
        # workspace.cleanup.on_closed_pr is False.
        self._sweep_for_closed_prs()

        # 1. Reconcile currently-active workers against fresh issue state.
        await self._reconcile(result)

        # 2. Fetch candidate issues + dispatch up to the concurrency limit.
        if result.dispatch_paused:
            _LOG.warning(
                "workflow reload invalid; pausing new dispatch until workflow is fixed"
            )
            return result
        candidates = self.tracker.fetch_candidate_issues()
        await self._dispatch(candidates, result)

        return result

    async def run_forever(self) -> None:  # pragma: no cover - simple loop
        """Convenience scheduler. Tests use ``run_once`` directly."""
        while True:
            await self.run_once()
            interval = max(0.001, self.config.polling.interval_ms / 1000)
            await asyncio.sleep(interval)

    def status_snapshot(self) -> dict[str, Any]:
        """Return the read-only runtime status snapshot (#55)."""
        return build_status_snapshot(self)

    def _maybe_reload_workflow(self):
        if self._workflow_reloader is None:
            return None
        result = self._workflow_reloader.maybe_reload()
        if result.reloaded:
            self._apply_workflow_config(result.current_snapshot.config)
        return result

    def _apply_workflow_config(self, config: WorkflowConfig) -> None:
        self.config = config
        self._cleanup = WorkspaceCleanupExecutor(
            self.workspaces,
            self.config.workspace.cleanup,
        )
        self._artifact_retention = ArtifactRetentionExecutor(
            self.config.claude.artifact_store,
            self.config.claude.artifact_retention,
            redact_keys=self.config.logging.redact_keys,
            clock=self._clock,
        )
        self._evidence = EvidenceDetector(
            self.config.github,
            client=getattr(self.tracker, "client", None),
        )
        self.workspaces.config = self.config.workspace
        if hasattr(self.tracker, "tracker"):
            self.tracker.tracker = self.config.tracker
        if hasattr(self.tracker, "github"):
            self.tracker.github = self.config.github
        if hasattr(self.tracker, "ready_label"):
            self.tracker.ready_label = (
                self.config.tracker.include_labels[0]
                if self.config.tracker.include_labels
                else ""
            )
        if hasattr(self.tracker, "claim_label"):
            self.tracker.claim_label = self.config.github.claim_label

    def _default_continuation_policy(self, worker: WorkerState) -> str | None:
        """Render the workflow prompt once, then stop."""
        if worker.turn_count != 0:
            return None
        prompt = self._render_first_prompt(worker)
        if worker.lane is not None:
            parts = [
                part
                for part in (worker.lane.prompt_prefix, prompt, worker.lane.prompt_suffix)
                if part
            ]
            return "\n\n".join(parts)
        return prompt

    def _render_first_prompt(self, worker: WorkerState) -> str:
        return self._render_prompt_for_issue(
            worker.issue,
            workspace_path=str(worker.workspace.path),
        )

    def _render_prompt_for_issue(self, issue: Issue, *, workspace_path: str) -> str:
        workflow = self._workflow_reloader.snapshot.workflow if self._workflow_reloader else None
        if workflow is None:
            return f"Work on {issue.identifier}."
        return render_prompt(
            workflow,
            issue=issue,
            extra={"workspace_path": workspace_path},
        )

    # -- Restart recovery (#31) ---------------------------------------------

    async def recover(self) -> list[RecoveryDecision]:
        """Reconcile persisted session records with tracker state at startup.

        Idempotent + safe to call before ``run_once``. Walks every
        in-flight :class:`SessionRecord` under ``claude.session_store``
        and, per ``claude.retry_resume_policy``, either resumes the
        worker, releases the claim for fresh dispatch, marks the issue
        blocked, or discards the orphan.

        See :mod:`symphony.recovery` for the detailed flow. Returned
        list is also stashed on ``self.recovery_decisions`` for the CLI's
        startup summary.
        """
        decisions: list[RecoveryDecision] = []
        store = self.config.claude.session_store
        records = discover_persisted_records(store)
        if not records:
            self.recovery_decisions = decisions
            return decisions

        policy = self.config.claude.retry_resume_policy
        _LOG.info(
            "recovery: found %d in-flight session record(s) under %s (policy=%s)",
            len(records),
            store,
            policy,
        )

        # Resolve fresh issue state in one batch where possible.
        numbers = sorted({rec.issue_number for _, rec in records})
        try:
            fresh_issues = {
                i.identifier: i for i in self.tracker.fetch_issues_by_numbers(numbers)
            }
        except TrackerError as exc:
            _LOG.warning(
                "recovery: tracker fetch failed (%s); treating all records as skipped",
                exc,
            )
            for path, record in records:
                decisions.append(
                    self._record_recovery_decision(
                        record_path=path,
                        record=record,
                        action=ACTION_SKIPPED,
                        reason=f"tracker fetch failed: {exc}",
                        policy=policy,
                    )
                )
            self.recovery_decisions = decisions
            return decisions

        for path, record in records:
            issue = fresh_issues.get(record.issue_identifier)
            decision = await self._recover_one(
                path=path,
                record=record,
                issue=issue,
                policy=policy,
            )
            decisions.append(decision)

        self.recovery_decisions = decisions
        return decisions

    async def _recover_one(
        self,
        *,
        path: Path,
        record: SessionRecord,
        issue: Issue | None,
        policy: str,
    ) -> RecoveryDecision:
        # Issue vanished entirely → release any stale claim by issue_number is
        # impossible without an Issue object. Just discard the record so we
        # do not loop on it forever.
        if issue is None:
            return self._record_recovery_decision(
                record_path=path,
                record=record,
                action=ACTION_DISCARDED,
                reason="issue not retrievable from tracker",
                policy=policy,
            )

        actionable, why_not = issue_is_actionable(
            issue,
            exclude_labels=self.config.tracker.exclude_labels,
            blocked_label=self.config.github.blocked_label,
        )
        if not actionable:
            self._best_effort_release(issue, "restart-recovery-ineligible")
            return self._record_recovery_decision(
                record_path=path,
                record=record,
                action=ACTION_RELEASED,
                reason=f"issue not eligible: {why_not}",
                policy=policy,
            )

        if policy == "fail_closed":
            self._best_effort_block(issue, "restart-recovery-fail-closed")
            return self._record_recovery_decision(
                record_path=path,
                record=record,
                action=ACTION_BLOCKED,
                reason="retry_resume_policy=fail_closed",
                policy=policy,
            )

        if policy == "new_session_with_summary":
            # Release claim so next dispatch tick re-claims cleanly with a
            # fresh session, and stash the prior provider session id chain
            # so the new session's continuation prompt can reference it.
            chain = list(record.previous_provider_session_ids)
            if record.provider_session_id:
                chain.append(record.provider_session_id)
            if chain:
                self._restart_carryover[issue.identifier] = chain
            self._best_effort_release(issue, "restart-recovery-new-session")
            return self._record_recovery_decision(
                record_path=path,
                record=record,
                action=ACTION_RELEASED,
                reason=(
                    "retry_resume_policy=new_session_with_summary;"
                    " fresh dispatch will inherit summary chain"
                ),
                policy=policy,
            )

        # Default policy: resume_same_session.
        if not record.provider_session_id:
            # Nothing to resume against. Mark blocked rather than silently
            # discarding — operator should know the daemon could not
            # honor the configured policy.
            self._best_effort_block(
                issue,
                "restart-recovery-no-provider-session-id",
            )
            return self._record_recovery_decision(
                record_path=path,
                record=record,
                action=ACTION_BLOCKED,
                reason="resume_same_session requires provider_session_id; record has none",
                policy=policy,
            )
        try:
            session = await self.provider.restore(record)
        except ProviderRestoreError as exc:
            _LOG.warning(
                "recovery: restore failed for %s (%s); marking blocked",
                issue.identifier,
                exc,
            )
            self._best_effort_block(issue, f"restart-recovery-restore-failed: {exc}")
            return self._record_recovery_decision(
                record_path=path,
                record=record,
                action=ACTION_BLOCKED,
                reason=f"provider restore failed: {exc}",
                policy=policy,
            )

        # Restore succeeded. Build a worker, drop into self.active so the
        # next run_once() doesn't double-dispatch, then drive it inline so
        # recovery is observable from the same call.
        workspace = self.workspaces.prepare(issue)
        self.workspaces.run_hook("before_run", workspace)

        artifacts = ArtifactWriter.for_attempt(
            self.config.claude.artifact_store,
            owner=issue.owner,
            repo=issue.repo,
            issue_number=issue.number,
            attempt=session.attempt,
            redact_keys=self.config.logging.redact_keys,
        )
        artifacts.write_json(
            "request.json",
            {
                "issue_identifier": issue.identifier,
                "workspace_path": str(workspace.path),
                "attempt": session.attempt,
                "model": self.config.claude.model,
                "permission_mode": self.config.claude.permission_mode,
                "security_profile": self.config.security.profile,
                "run_id": self.run_id,
                "recovered_from_session": record.session_id,
            },
        )
        session.artifact_dir = artifacts.root
        artifacts.write_json("session.json", _session_snapshot(session))

        worker = WorkerState(
            issue=issue,
            workspace=workspace,
            session=session,
            artifacts=artifacts,
            config=self.config,
        )
        self.active[issue.identifier] = worker
        decision = self._record_recovery_decision(
            record_path=path,
            record=record,
            action=ACTION_RESUMED,
            reason="restored via provider.restore()",
            policy=policy,
            restored_session_id=session.session_id,
            artifacts=artifacts,
        )
        # Drive the resumed worker inline. Pass a synthetic claim and a
        # local TickResult — recovery's _run_worker call belongs to the
        # recovery cycle, not to a dispatch tick, so its retries land on
        # ``self.retry_states`` regardless of the throwaway result object.
        await self._run_worker(worker, TickResult(), ClaimResult(ok=True))
        # Stamp the on-disk record with the resumed worker's final state
        # so a subsequent recover() does not re-process it. Real-provider
        # ``_persist_session`` would already have updated the file
        # mid-run; the fake provider doesn't, and operators may also have
        # session stores written by older daemons missing the
        # ``terminal_state`` patch — defensive stamp covers both.
        try:
            stamped = worker.terminal_state or Terminal.COMPLETED
            _stamp_record_terminal(path, record, stamped)
        except OSError as exc:
            _LOG.warning(
                "recovery: post-resume stamp failed for %s: %s",
                path,
                exc,
            )
        return decision

    def _record_recovery_decision(
        self,
        *,
        record_path: Path,
        record: SessionRecord,
        action: str,
        reason: str,
        policy: str,
        restored_session_id: str | None = None,
        artifacts: ArtifactWriter | None = None,
    ) -> RecoveryDecision:
        """Build the decision object, persist a recovery.json artifact, and
        update the on-disk record so subsequent recover() calls do not
        re-process it.
        """
        decision = RecoveryDecision(
            record_path=record_path,
            issue_identifier=record.issue_identifier,
            issue_number=record.issue_number,
            action=action,
            reason=reason,
            policy=policy,
            restored_session_id=restored_session_id,
        )

        # Pick an artifact writer:
        # - "resumed" path supplies its own (already opened the new attempt dir);
        # - other actions write under the record's last-known attempt dir so
        #   operators find the trail next to the prior session.json.
        if artifacts is None:
            artifacts = ArtifactWriter(
                record.artifact_dir,
                redact_keys=self.config.logging.redact_keys,
            )
        try:
            artifacts.write_json("recovery.json", decision.to_json())
        except OSError as exc:
            _LOG.warning(
                "recovery: could not write recovery.json under %s: %s",
                artifacts.root,
                exc,
            )

        # For terminal actions (released / blocked / discarded / skipped),
        # stamp the on-disk record so subsequent restarts skip it.
        if action != ACTION_RESUMED:
            stamped = Terminal.CANCELLED if action == ACTION_RELEASED else Terminal.FAILED
            if action == ACTION_DISCARDED:
                stamped = Terminal.CANCELLED
            try:
                _stamp_record_terminal(record_path, record, stamped)
            except OSError as exc:
                _LOG.warning(
                    "recovery: could not stamp record %s as %s: %s",
                    record_path,
                    stamped,
                    exc,
                )

        _LOG.info(
            "recovery: %s %s (%s) — %s",
            action,
            record.issue_identifier,
            policy,
            reason,
        )
        return decision

    def _best_effort_release(self, issue: Issue, reason: str) -> None:
        try:
            self.tracker.release_issue(issue, reason)
        except TrackerError as exc:
            _LOG.warning(
                "recovery: release_issue failed for %s: %s",
                issue.identifier,
                exc,
            )

    def _best_effort_block(self, issue: Issue, reason: str) -> None:
        try:
            self.tracker.mark_issue_blocked(issue, reason)
        except TrackerError as exc:
            _LOG.warning(
                "recovery: mark_issue_blocked failed for %s: %s",
                issue.identifier,
                exc,
            )

    # -- Reconciliation -----------------------------------------------------

    def _sweep_for_closed_prs(self) -> None:
        """Per-tick sweep that wires :meth:`cleanup_for_closed_pr` (#82).

        For each persisted session record whose workspace path still
        exists and whose issue is NOT currently active:

        - Reconstruct a minimal :class:`Issue` from session metadata.
        - Look up linked PRs via :meth:`tracker.find_linked_pull_requests`.
        - If ANY linked PR is still ``"open"``, preserve the workspace
          (executor returns ``KEPT_PR_OPEN``).
        - If linked PRs exist and are ALL ``"closed"``/``"merged"``,
          delete the workspace via :meth:`cleanup_for_closed_pr`,
          preferring the ``"merged"`` state when both occur.
        - Empty PR list → preserve (no terminal evidence).

        Tracker errors are caught per-workspace so one bad PR lookup
        does not abort the sweep. The sweep is a no-op when
        ``workspace.cleanup.enabled`` or ``on_closed_pr`` is False.
        """
        cleanup_cfg = self.config.workspace.cleanup
        if not cleanup_cfg.enabled or not cleanup_cfg.on_closed_pr:
            return
        active_identifiers = set(self.active.keys())
        for _record_path, record in discover_workspace_records(
            self.config.claude.session_store
        ):
            if record.issue_identifier in active_identifiers:
                continue
            if not record.workspace_path.is_dir():
                continue
            synthetic_issue = _issue_from_session_record(record)
            if synthetic_issue is None:
                _LOG.warning(
                    "closed-pr sweep: malformed session issue identifier %s — skipping",
                    record.issue_identifier,
                )
                continue
            try:
                prs = self.tracker.find_linked_pull_requests(synthetic_issue)
            except TrackerError as exc:
                _LOG.warning(
                    "closed-pr sweep: tracker lookup failed for %s: %s — skipping",
                    record.issue_identifier,
                    exc,
                )
                continue
            if not prs:
                continue
            workspace = Workspace(
                issue_identifier=synthetic_issue.identifier,
                workspace_key=record.workspace_path.name,
                path=record.workspace_path,
                repo_path=record.workspace_path,
                created_at=record.started_at,
                reused=True,
            )
            if any(p.state == "open" for p in prs):
                # Defer to the executor for the KEPT_PR_OPEN decision +
                # log line so operators see one consistent surface.
                self._cleanup.cleanup_for_closed_pr(workspace, pr_state="open")
                continue
            # All PRs terminal; prefer "merged" over "closed" for the
            # decision reason.
            pr_state = "merged" if any(p.state == "merged" for p in prs) else "closed"
            self._cleanup.cleanup_for_closed_pr(workspace, pr_state=pr_state)

    async def _reconcile(self, result: TickResult) -> None:
        if not self.active:
            return
        identifiers = list(self.active.keys())
        numbers = [self.active[i].issue.number for i in identifiers]
        try:
            fresh = {i.identifier: i for i in self.tracker.fetch_issues_by_numbers(numbers)}
        except TrackerError as exc:
            _LOG.warning("reconcile fetch failed: %s", exc)
            return

        for identifier in identifiers:
            issue = fresh.get(identifier)
            if issue is None:
                continue
            worker = self.active[identifier]
            worker_config = _worker_config(worker, self.config)
            became_ineligible = (
                issue.state != "open"
                or worker_config.tracker.exclude_labels
                and any(lbl in issue.labels for lbl in worker_config.tracker.exclude_labels)
            )
            if became_ineligible:
                _LOG.info("reconcile: cancelling worker for %s", identifier)
                await self._cancel_worker(worker, reason="reconcile-ineligible")
                self.active.pop(identifier, None)
                result.reconciled_cancelled.append(identifier)

    async def _cancel_worker(self, worker: WorkerState, *, reason: str) -> None:
        try:
            event = await self.provider.cancel(worker.session)
            self._record_event(worker, event)
        except ProviderError as exc:
            _LOG.warning("cancel failed for %s: %s", worker.issue.identifier, exc)
        worker.terminal_state = Terminal.CANCELLED
        try:
            self.tracker.release_issue(worker.issue, reason)
        except TrackerError as exc:
            _LOG.warning("release after cancel failed: %s", exc)

    # -- Dispatch -----------------------------------------------------------

    async def _dispatch(self, candidates: Iterable[Issue], result: TickResult) -> None:
        slots_open = self.config.agent.max_concurrency - len(self.active)
        if slots_open <= 0:
            return
        now = self._clock()
        local_runs = []
        for issue in candidates:
            if slots_open == 0:
                break
            lane = self._select_lane(issue)
            if lane is None and self.config.lanes:
                continue
            if self.config.lanes:
                if not self._lane_has_capacity(lane):
                    continue
            if issue.identifier in self.active:
                continue
            retry = self.retry_states.get(issue.identifier)
            if retry is not None and not retry.should_run(now=now):
                continue

            # Claim BEFORE provider start. If claim fails, do not run.
            run_metadata = {
                "run_id": self.run_id,
                "started_at": now.isoformat(),
            }
            try:
                claim = self.tracker.claim_issue(issue, run_metadata)
            except TrackerError as exc:
                _LOG.warning("claim raised for %s: %s", issue.identifier, exc)
                continue
            if not claim.ok:
                if claim.conflict:
                    result.skipped_claim_conflict.append(issue.identifier)
                continue

            if self.config.remote.enabled:
                await self._run_remote_dispatch(issue, retry=retry, result=result)
                slots_open -= 1
                continue

            try:
                worker = await self._start_worker(issue, retry=retry, lane=lane)
            except ProviderRetryableError as exc:
                # Restore-startup failure under resume_same_session, or any
                # other startup-time retryable provider error. Schedule a
                # retry per RetryConfig.
                _LOG.warning("start_worker retryable failure for %s: %s", issue.identifier, exc)
                self.tracker.release_issue(issue, "start-failed-retryable")
                self._on_worker_failed(issue, str(exc), retryable=True)
                result.retries_scheduled.append(issue.identifier)
                continue
            except Exception as exc:  # noqa: BLE001 - every other failure releases the claim
                _LOG.exception("start_worker failed for %s", issue.identifier)
                self.tracker.release_issue(issue, "start-failed")
                self._on_worker_failed(issue, str(exc), retryable=False)
                continue
            self.active[issue.identifier] = worker
            result.dispatched.append(issue.identifier)
            slots_open -= 1
            local_runs.append(self._run_worker(worker, result, claim))

        if local_runs:
            await asyncio.gather(*local_runs)

    def _select_lane(self, issue: Issue) -> LaneConfig | None:
        if not self.config.lanes:
            return None
        labels = set(issue.labels)
        for lane in self.config.lanes:
            if _issue_matches_lane(issue, lane):
                return lane
        _LOG.debug("no lane matched %s labels=%s", issue.identifier, sorted(labels))
        return None

    def _lane_has_capacity(self, lane: LaneConfig | None) -> bool:
        if lane is None:
            return False
        limit = lane.max_concurrency or self.config.agent.max_concurrency
        active = sum(
            1
            for worker in self.active.values()
            if worker.lane is not None and worker.lane.name == lane.name
        )
        return active < limit

    async def _run_remote_dispatch(
        self,
        issue: Issue,
        *,
        retry: RetryState | None,
        result: TickResult,
    ) -> None:
        attempt = (retry.attempts + 1) if retry else 1
        artifacts = ArtifactWriter.for_attempt(
            self.config.claude.artifact_store,
            owner=issue.owner,
            repo=issue.repo,
            issue_number=issue.number,
            attempt=attempt,
            redact_keys=self.config.logging.redact_keys,
        )
        artifacts.write_json(
            "request.json",
            {
                "issue_identifier": issue.identifier,
                "attempt": attempt,
                "execution": "remote",
                "security_profile": self.config.security.profile,
                "run_id": self.run_id,
            },
        )

        result.dispatched.append(issue.identifier)
        remote_result: RemoteDispatchRunResult | None = None
        errors: tuple[str, ...] = ()
        terminal_state = Terminal.COMPLETED
        try:
            if self.remote_dispatcher is None:
                raise RuntimeError("remote.enabled=true but no remote dispatcher is configured")
            remote_result = self.remote_dispatcher.dispatch(
                issue,
                attempt=attempt,
                config=self.config,
                prompt=self._render_prompt_for_issue(issue, workspace_path=""),
            )
            errors = _redact_error_texts(remote_result.errors, self.config)
            if remote_result.failed:
                terminal_state = Terminal.FAILED
        except Exception as exc:  # noqa: BLE001 - remote boundary must fail closed
            terminal_state = Terminal.FAILED
            errors = (
                _redact_error_text(
                    f"remote dispatch failed: {exc}",
                    self.config,
                ),
            )

        retryable = terminal_state != Terminal.COMPLETED
        if retryable:
            self._on_worker_failed(issue, "; ".join(errors), retryable=True)
            retry_state = self.retry_states.get(issue.identifier)
            if retry_state is not None and retry_state.next_attempt_at is not None:
                result.retries_scheduled.append(issue.identifier)
        else:
            retry_state = self.retry_states.pop(issue.identifier, None)
            if retry_state is not None:
                retry_state.record_success(now=self._clock())

        artifacts.write_json(
            "remote-dispatch.json",
            {
                "ok": remote_result.ok if remote_result is not None else False,
                "failed": remote_result.failed if remote_result is not None else True,
                "errors": errors,
                "transport_failed": (
                    remote_result.transport.failed
                    if remote_result is not None and remote_result.transport is not None
                    else None
                ),
                "transport_stalled": (
                    remote_result.transport.stalled
                    if remote_result is not None and remote_result.transport is not None
                    else None
                ),
                "artifacts_partial": (
                    remote_result.artifacts.partial
                    if remote_result is not None and remote_result.artifacts is not None
                    else None
                ),
            },
        )

        should_block = False
        if retryable:
            retry_state = self.retry_states.get(issue.identifier)
            should_block = (
                retry_state is not None
                and retry_state.attempts > 0
                and retry_state.next_attempt_at is None
            )

        artifacts.write_json(
            "terminal.json",
            {
                "security_profile": self.config.security.profile,
                "terminal_state": terminal_state.value,
                "reason": "remote_completed" if not retryable else "remote_failed",
                "retryable": retryable and not should_block,
                "blocked": should_block,
                "error": "; ".join(errors) if errors else None,
                "attempt": attempt,
                "execution": "remote",
            },
        )

        if should_block:
            try:
                self.tracker.mark_issue_blocked(
                    issue,
                    "; ".join(errors) or "remote dispatch failed",
                )
            except Exception as exc:  # noqa: BLE001 - tracker errors must not mask outcome
                _LOG.warning("mark_issue_blocked failed for %s: %s", issue.identifier, exc)
        else:
            try:
                self.tracker.release_issue(
                    issue,
                    "remote_completed" if not retryable else "remote_failed",
                )
            except Exception as exc:  # noqa: BLE001 - tracker errors must not mask outcome
                _LOG.warning("release_issue failed for %s: %s", issue.identifier, exc)

        result.finished.append(issue.identifier)

    async def _start_worker(
        self,
        issue: Issue,
        *,
        retry: RetryState | None,
        lane: LaneConfig | None = None,
    ) -> WorkerState:
        workspace = self.workspaces.prepare(issue)

        # Run after_create only on first creation per SPEC §8.
        if not workspace.reused:
            self.workspaces.run_hook("after_create", workspace)
        self.workspaces.run_hook("before_run", workspace)

        attempt = (retry.attempts + 1) if retry else 1
        artifacts = ArtifactWriter.for_attempt(
            self.config.claude.artifact_store,
            owner=issue.owner,
            repo=issue.repo,
            issue_number=issue.number,
            attempt=attempt,
            redact_keys=self.config.logging.redact_keys,
        )
        artifacts.write_json(
            "request.json",
            {
                "issue_identifier": issue.identifier,
                "workspace_path": str(workspace.path),
                "attempt": attempt,
                "model": self.config.claude.model,
                "permission_mode": self.config.claude.permission_mode,
                "security_profile": self.config.security.profile,
                "run_id": self.run_id,
                "lane": _lane_summary(lane),
            },
        )

        if retry is not None and retry.attempts > 0:
            # Cross-attempt restore path. Use the existing session record
            # off the prior worker if we kept one around — for the
            # default-stateless orchestrator we re-create a record and
            # let the provider's restore() do its thing.
            stale = WorkerState(
                issue=issue,
                workspace=workspace,
                session=SessionRecord(
                    session_id=f"sym-{uuid.uuid4().hex[:12]}",
                    provider=self.provider.name,
                    issue_identifier=issue.identifier,
                    issue_number=issue.number,
                    workspace_path=workspace.path,
                    artifact_dir=artifacts.root,
                    started_at=self._clock(),
                    attempt=attempt - 1,  # restore() bumps to `attempt`
                ),
                artifacts=artifacts,
                config=self.config,
            )
            try:
                session = await self.provider.restore(stale.session)
            except ProviderRestoreError as exc:
                # Honor claude.retry_resume_policy per docs/claude-provider.md
                # §5.3. Previous version unconditionally fell back to
                # start_session, which was wrong for resume_same_session and
                # fail_closed.
                policy = self.config.claude.retry_resume_policy
                _LOG.warning(
                    "restore failed for %s under policy=%s: %s",
                    issue.identifier,
                    policy,
                    exc,
                )
                if policy == "resume_same_session":
                    # Caller routes ProviderRetryableError to RetryConfig.
                    raise ProviderRetryableError(
                        f"restore failed under resume_same_session: {exc}"
                    ) from exc
                if policy == "fail_closed":
                    # Re-raise as a non-retryable provider error.
                    raise ProviderError(f"restore failed under fail_closed: {exc}") from exc
                if policy == "new_session_with_summary":
                    # Only this policy may fall back to start_session.
                    # Preserve previous_provider_session_ids so #9's
                    # continuation prompt can carry summary handoff.
                    prev_ids = list(stale.session.previous_provider_session_ids)
                    if stale.session.provider_session_id:
                        prev_ids.append(stale.session.provider_session_id)
                    session = await self.provider.start_session(
                        issue, workspace.path, self.config.claude
                    )
                    session.attempt = attempt
                    session.previous_provider_session_ids = prev_ids
                else:  # pragma: no cover - config validator rejects unknown
                    raise ProviderError(f"unknown retry_resume_policy: {policy!r}") from exc
        else:
            session = await self.provider.start_session(issue, workspace.path, self.config.claude)
            session.attempt = attempt
            # Restart-recovery handoff: when the prior daemon ran under
            # ``new_session_with_summary`` and its in-flight session was
            # released for fresh dispatch, ``recover()`` stashed the prior
            # provider session id chain. Drain it onto this fresh session
            # so the next continuation prompt can reference the prior
            # conversation.
            carry = self._restart_carryover.pop(issue.identifier, None)
            if carry:
                session.previous_provider_session_ids = list(carry)
        session.artifact_dir = artifacts.root

        artifacts.write_json("session.json", _session_snapshot(session))
        return WorkerState(
            issue=issue,
            workspace=workspace,
            session=session,
            artifacts=artifacts,
            config=self.config,
            lane=lane,
        )

    async def _run_worker(
        self,
        worker: WorkerState,
        result: TickResult,
        claim: ClaimResult,
    ) -> None:
        del claim  # claim is informational; release happens regardless of outcome
        worker_config = _worker_config(worker, self.config)
        max_turns = worker_config.agent.max_turns
        try:
            # `turn_completed` ends ONE turn but not the whole session — the
            # orchestrator may keep sending continuation prompts up to
            # max_turns. Only `turn_failed` / `turn_cancelled` (or an
            # exception, or the policy returning None) breaks the loop.
            while worker.turn_count < max_turns:
                message = self.continuation_policy(worker)
                if message is None:
                    break
                terminal = await self._run_one_turn(worker, message)
                worker.turn_count += 1
                if terminal == "turn_cancelled" and worker.timeout_subtype:
                    # Timeout-induced cancellation is retryable per SPEC §16.
                    self._on_worker_failed(
                        worker.issue, worker.error or worker.timeout_subtype, retryable=True
                    )
                    result.retries_scheduled.append(worker.issue.identifier)
                    break
                if terminal == "turn_failed":
                    # SDK delivered ResultMessage(is_error=True). Per SPEC §16,
                    # provider failures default to retryable; only specific
                    # auth/permission subtypes flip to non-retryable. The
                    # _run_worker finally block then either schedules a
                    # retry or calls mark_issue_blocked depending on which
                    # branch _on_worker_failed routes through. See #30.
                    retryable, reason = _classify_turn_failed(worker.last_event)
                    worker.error = reason
                    self._on_worker_failed(worker.issue, reason, retryable=retryable)
                    if retryable:
                        result.retries_scheduled.append(worker.issue.identifier)
                    break
                if terminal == "turn_cancelled":
                    # Provider-emitted cancel without timeout context (e.g.
                    # operator interrupt that already fired before we got
                    # back to the loop). Treat as retryable so the issue
                    # can be re-dispatched on the next tick.
                    self._on_worker_failed(
                        worker.issue, worker.error or "turn_cancelled", retryable=True
                    )
                    result.retries_scheduled.append(worker.issue.identifier)
                    break
                if terminal == "no_terminal":
                    # The provider's send_input stream ended without
                    # emitting a terminal turn event. Treat it as a
                    # crash — we cannot safely keep prompting on a
                    # session whose state is unknown.
                    worker.error = "provider stream ended without terminal event"
                    worker.terminal_state = Terminal.CRASHED
                    self._on_worker_failed(worker.issue, worker.error, retryable=True)
                    result.retries_scheduled.append(worker.issue.identifier)
                    break
            if worker.terminal_state is None:
                worker.terminal_state = Terminal.COMPLETED
        except ProviderRetryableError as exc:
            worker.error = str(exc)
            worker.terminal_state = Terminal.FAILED
            self._on_worker_failed(worker.issue, str(exc), retryable=True)
            result.retries_scheduled.append(worker.issue.identifier)
        except ProviderError as exc:
            worker.error = str(exc)
            worker.terminal_state = Terminal.FAILED
            self._on_worker_failed(worker.issue, str(exc), retryable=False)
        finally:
            self.workspaces.run_hook("after_run", worker.workspace)
            try:
                await self.provider.close(worker.session)
            except ProviderError as exc:
                _LOG.warning("close failed: %s", exc)

            # Outcome routing: non-retryable failures are marked blocked
            # so a future operator (or run) sees the issue is broken; all
            # other outcomes (success, retryable failure, timeout, crash,
            # reconcile-cancel) just release the claim and let the
            # retry-state machine drive what comes next.
            #
            # SPEC §17.2 / #62 (M5.3) extension: clean provider runs whose
            # task_outcome is `incomplete_no_evidence` or
            # `incomplete_permission_denied` are also blocked. The provider
            # said "done" but Symphony observed no PR / no declaration / no
            # successful tool calls — auto-retrying would just spin, so the
            # operator must intervene (change permission_mode, fix the
            # workflow, or close the issue). The detector's `unknown`
            # outcome (no GitHubClient wired, can't verify) is intentionally
            # NOT in the block-set — we don't escalate runs we couldn't
            # verify.
            outcome_reason = worker.terminal_state.value if worker.terminal_state else "ended"
            retry = self.retry_states.get(worker.issue.identifier)
            non_retryable_failure = (
                worker.terminal_state == Terminal.FAILED
                and retry is not None
                and retry.attempts > 0
                and retry.next_attempt_at is None
            )

            # Run the evidence detector BEFORE the routing decision so its
            # task_outcome can drive blocking for misleading-success runs.
            permission_denials_count = _extract_permission_denials_count(worker)
            detector_result = self._evidence.detect(
                issue=worker.issue,
                terminal_state=worker.terminal_state,
                retryable=_is_retryable(worker, retry),
                blocked=non_retryable_failure,
                permission_denials_count=permission_denials_count,
                last_event=worker.last_event,
                # M5.2 detector reads last_event payload directly;
                # M5.4 may pass a longer assistant-text tail.
                recent_assistant_text="",
                workspace_path=worker.workspace.path,
            )
            completion_blocked = detector_result.task_outcome in {
                OUTCOME_INCOMPLETE_NO_EVIDENCE,
                OUTCOME_INCOMPLETE_PERMISSION_DENIED,
            }
            should_block = non_retryable_failure or completion_blocked
            _maybe_log_task_outcome(worker, detector_result)

            if should_block:
                block_reason = (
                    worker.error
                    or f"task_outcome={detector_result.task_outcome}"
                )
                _comment_blocked_outcome(
                    self.tracker,
                    worker=worker,
                    detector_result=detector_result,
                    block_reason=block_reason,
                    config=self.config,
                )
                try:
                    self.tracker.mark_issue_blocked(worker.issue, block_reason)
                except Exception as exc:  # noqa: BLE001 - tracker errors must not mask outcome
                    _LOG.warning(
                        "mark_issue_blocked failed for %s: %s",
                        worker.issue.identifier,
                        exc,
                    )
            else:
                try:
                    self.tracker.release_issue(worker.issue, outcome_reason)
                except Exception as exc:  # noqa: BLE001 - same rationale
                    _LOG.warning(
                        "release_issue failed for %s: %s",
                        worker.issue.identifier,
                        exc,
                    )

            worker.artifacts.write_json(
                "terminal.json",
                {
                    "security_profile": _worker_config(
                        worker,
                        self.config,
                    ).security.profile,
                    "terminal_state": (
                        worker.terminal_state.value if worker.terminal_state else "ended"
                    ),
                    "reason": _terminal_reason(worker),
                    "retryable": _is_retryable(worker, retry),
                    "subtype": worker.timeout_subtype,
                    # `blocked` reflects the unified decision: either a
                    # non-retryable provider failure (existing semantics)
                    # OR a misleading-success run that #62 escalated. The
                    # `task_outcome` field below tells the operator which
                    # path triggered it.
                    "blocked": should_block,
                    "last_event_at": (worker.last_event.timestamp if worker.last_event else None),
                    "provider_session_id": worker.session.provider_session_id,
                    "error": worker.error,
                    "turn_count": worker.turn_count,
                    # SPEC §17 + #45: a turn that "completed" with one or
                    # more denied tool calls is misleading-success — Claude
                    # answered but could not perform the underlying action
                    # (typically because the operator chose acceptEdits and
                    # the agent needed Bash/AskUserQuestion). Count is
                    # surfaced so operators can grep terminal.json for
                    # incomplete runs without reparsing events.jsonl.
                    "permission_denials_count": permission_denials_count,
                    "usage": worker.usage.to_json() if worker.usage.has_usage else None,
                    # SPEC §17.1 task-outcome row (M5.1 #61, populated by
                    # the M5.2 #60 detector). Routing decisions remain
                    # driven by terminal_state / retryable / blocked —
                    # task_outcome is operator-visible signal only.
                    **detector_result.to_terminal_fields(),
                },
            )
            self.active.pop(worker.issue.identifier, None)
            result.finished.append(worker.issue.identifier)
            self._remember_finished(
                worker,
                detector_result,
                permission_denials_count=permission_denials_count,
            )
            if worker.terminal_state == Terminal.COMPLETED:
                rs = self.retry_states.pop(worker.issue.identifier, None)
                if rs is not None:
                    rs.record_success(now=self._clock())
                # M5.7 #66: terminal-issue cleanup trigger. Only fires
                # for clean task outcomes (completed_with_pr /
                # completed_no_pr_declared) — incomplete_* runs already
                # routed to mark_issue_blocked above and the operator
                # needs the workspace preserved for inspection. No-op
                # when workspace.cleanup.enabled or on_terminal_issue
                # is False.
                if detector_result.task_outcome in {
                    OUTCOME_COMPLETED_WITH_PR,
                    OUTCOME_COMPLETED_NO_PR_DECLARED,
                }:
                    self._cleanup.cleanup_for_terminal_issue(worker.workspace)

    async def _run_one_turn(self, worker: WorkerState, message: str) -> str:
        """Drive one send_input turn to its terminal event.

        Returns the terminal event name (``turn_completed`` /
        ``turn_failed`` / ``turn_cancelled``) or ``"no_terminal"`` if the
        stream ended without one (treated as a crash by the caller).

        ``turn_completed`` does NOT set ``worker.terminal_state`` because
        a successful turn does not end the session — the orchestrator may
        send another continuation prompt.

        Two timeouts are enforced inline (SPEC §11, docs/claude-provider.md §6):

        - ``claude.stall_timeout_ms`` — wallclock since the last
          *content-bearing* event. On expiry the provider is interrupted
          and the synthesized ``turn_cancelled`` event carries
          ``payload.subtype = "stall_timeout"``.
        - ``claude.turn_timeout_ms`` — wallclock since the call started.
          Same interrupt path with ``payload.subtype = "turn_timeout"``.

        Both timeouts mark the worker retryable per SPEC §16.
        """
        worker_config = _worker_config(worker, self.config)
        stall_ms = worker_config.claude.stall_timeout_ms
        turn_ms = worker_config.claude.turn_timeout_ms
        loop = asyncio.get_running_loop()
        turn_start = loop.time()
        last_event_time = turn_start

        terminal = "no_terminal"
        iterator = self.provider.send_input(worker.session, message).__aiter__()
        try:
            while True:
                now = loop.time()
                turn_elapsed_ms = (now - turn_start) * 1000
                stall_elapsed_ms = (now - last_event_time) * 1000

                # Cap the next await at the smaller of the two remaining
                # budgets so whichever fires first wins.
                turn_remaining_s = max(0.0, (turn_ms - turn_elapsed_ms) / 1000)
                stall_remaining_s = max(0.0, (stall_ms - stall_elapsed_ms) / 1000)
                wait_s = min(turn_remaining_s, stall_remaining_s)
                if wait_s <= 0:
                    # Already over budget before we even tried to read.
                    subtype = "turn_timeout" if turn_elapsed_ms >= turn_ms else "stall_timeout"
                    await self._on_turn_timeout(worker, subtype)
                    terminal = "turn_cancelled"
                    break

                try:
                    event = await asyncio.wait_for(iterator.__anext__(), timeout=wait_s)
                except asyncio.TimeoutError:
                    # Decide which budget tripped first.
                    now = loop.time()
                    if (now - turn_start) * 1000 >= turn_ms:
                        subtype = "turn_timeout"
                    else:
                        subtype = "stall_timeout"
                    await self._on_turn_timeout(worker, subtype)
                    terminal = "turn_cancelled"
                    break
                except StopAsyncIteration:
                    # Stream ended without terminal event — caller treats as crash.
                    break

                self._record_event(worker, event)
                last_event_time = loop.time()

                if event.event in TERMINAL_TURN_EVENTS:
                    terminal = event.event
                    if event.event == "turn_failed":
                        worker.terminal_state = Terminal.FAILED
                    elif event.event == "turn_cancelled":
                        worker.terminal_state = Terminal.CANCELLED
                    # turn_completed: leave terminal_state untouched.
                    break
        finally:
            # Best-effort generator cleanup so the SDK subprocess (or fake
            # state) is not left hanging when we time out mid-stream.
            close = getattr(iterator, "aclose", None)
            if close is not None:
                try:
                    await close()
                except Exception:  # noqa: BLE001 - cleanup must not mask the original outcome
                    _LOG.debug("generator aclose raised during turn cleanup", exc_info=True)
        return terminal

    async def _on_turn_timeout(self, worker: WorkerState, subtype: str) -> None:
        """Interrupt the in-flight turn and record a synthesized cancel event.

        Provider interrupt failures are logged; the orchestrator still
        marks the worker cancelled so the worker loop terminates cleanly.
        """
        try:
            await self.provider.interrupt(worker.session)
        except ProviderError as exc:
            _LOG.warning(
                "interrupt during %s for %s failed: %s",
                subtype,
                worker.issue.identifier,
                exc,
            )
        worker.terminal_state = Terminal.CANCELLED
        worker.timeout_subtype = subtype
        budget_ms = (
            self.config.claude.turn_timeout_ms
            if subtype == "turn_timeout"
            else self.config.claude.stall_timeout_ms
        )
        worker.error = f"{subtype} after {budget_ms}ms"
        # Synthesize a turn_cancelled event so events.jsonl reflects the
        # timeout — provider may not get a chance to emit one.
        self._record_event(
            worker,
            AgentEvent(
                event="turn_cancelled",
                timestamp=self._clock(),
                session_id=worker.session.session_id,
                provider=self.provider.name,
                issue_identifier=worker.issue.identifier,
                attempt=worker.session.attempt,
                payload={"subtype": subtype, "source": "orchestrator"},
                provider_session_id=worker.session.provider_session_id,
            ),
        )

    # -- Failure / retry ----------------------------------------------------

    def _on_worker_failed(self, issue: Issue, error: str, *, retryable: bool) -> None:
        rs = self.retry_states.setdefault(
            issue.identifier, RetryState(issue_identifier=issue.identifier)
        )
        if not retryable:
            rs.record_failure(error, now=self._clock(), backoff_ms=0)
            rs.next_attempt_at = None  # do not reschedule
            return
        next_attempt = (rs.attempts + 1) if rs.attempts < self.config.retry.max_attempts else 0
        if next_attempt == 0:
            rs.record_failure(error, now=self._clock(), backoff_ms=0)
            rs.next_attempt_at = None  # exhausted retries
            return
        backoff = next_backoff_ms(self.config.retry, attempt=next_attempt)
        rs.record_failure(error, now=self._clock(), backoff_ms=backoff)

    # -- Event recording ----------------------------------------------------

    def _record_event(self, worker: WorkerState, event: AgentEvent) -> None:
        worker.last_event = event
        worker.recent_events.append(event)
        del worker.recent_events[:-20]
        worker.session.last_event_at = event.timestamp
        worker.artifacts.append_event(event)
        if worker.usage.apply_event(event):
            worker.artifacts.write_json("usage.json", worker.usage.to_json())

    def _remember_finished(
        self,
        worker: WorkerState,
        detector_result: DetectorResult,
        *,
        permission_denials_count: int,
    ) -> None:
        self.recent_finished.append(
            {
                "issue_identifier": worker.issue.identifier,
                "issue_url": worker.issue.url,
                "artifact_dir": str(worker.artifacts.root),
                "session_id": worker.session.session_id,
                "provider_session_id": worker.session.provider_session_id,
                "attempt": worker.session.attempt,
                "lane": worker.lane.name if worker.lane else None,
                "security_profile": _worker_config(worker, self.config).security.profile,
                "terminal_state": (
                    worker.terminal_state.value if worker.terminal_state else None
                ),
                "task_outcome": detector_result.task_outcome,
                "outcome_decided_by": detector_result.outcome_decided_by,
                "task_evidence": detector_result.task_evidence,
                "no_pr_reason": detector_result.no_pr_reason,
                "permission_denials_count": permission_denials_count,
                "last_event_at": (
                    worker.last_event.timestamp.isoformat()
                    if worker.last_event is not None
                    else None
                ),
                "error": worker.error,
                "usage": worker.usage.to_json() if worker.usage.has_usage else None,
            }
        )
        del self.recent_finished[:-50]


# -- Helpers ------------------------------------------------------------------


def _issue_matches_lane(issue: Issue, lane: LaneConfig) -> bool:
    labels = set(issue.labels)
    if "do-not-claim" in labels:
        return False
    if "leader-owned" in labels and lane.name != "leader":
        return False
    if lane.include_labels and not all(label in labels for label in lane.include_labels):
        return False
    if any(label in labels for label in lane.exclude_labels):
        return False
    return True


def _lane_summary(lane: LaneConfig | None) -> dict[str, Any] | None:
    if lane is None:
        return None
    return {
        "name": lane.name,
        "include_labels": list(lane.include_labels),
        "exclude_labels": list(lane.exclude_labels),
        "max_concurrency": lane.max_concurrency,
        "prompt_prefix": bool(lane.prompt_prefix),
        "prompt_suffix": bool(lane.prompt_suffix),
    }


def _terminal_reason(worker: WorkerState) -> str:
    """One-token reason summary for ``terminal.json``.

    Composes ``terminal_state`` with ``timeout_subtype`` so operators can
    grep for ``"stall_timeout"`` / ``"turn_timeout"`` without parsing the
    whole record. Falls back to ``"ended"`` if no state was set
    (shouldn't happen — finally always sets one).
    """
    if worker.timeout_subtype:
        return worker.timeout_subtype
    if worker.terminal_state is None:
        return "ended"
    return worker.terminal_state.value


def _redact_error_texts(
    errors: tuple[str, ...],
    config: WorkflowConfig,
) -> tuple[str, ...]:
    return tuple(_redact_error_text(error, config) for error in errors)


def _redact_error_text(error: str, config: WorkflowConfig) -> str:
    return redact_text(
        error,
        redact_keys=config.logging.redact_keys,
        extra_secrets=(config.tracker.token,),
    )


def _is_retryable(worker: WorkerState, retry: RetryState | None) -> bool:
    """Did the orchestrator schedule another attempt for this worker?

    ``retry.next_attempt_at`` is the source of truth — it's set by
    ``_on_worker_failed(retryable=True)`` and cleared by the
    ``fail_closed`` / non-retryable paths. Worker outcomes that are not
    failures (COMPLETED, reconcile-CANCELLED) are reported as
    ``retryable=False`` because there's nothing to retry.
    """
    if worker.terminal_state == Terminal.COMPLETED:
        return False
    if retry is None:
        return False
    return retry.next_attempt_at is not None


def _session_snapshot(session: SessionRecord) -> dict[str, Any]:
    return {
        "session_id": session.session_id,
        "provider": session.provider,
        "provider_session_id": session.provider_session_id,
        "issue_identifier": session.issue_identifier,
        "issue_number": session.issue_number,
        "workspace_path": str(session.workspace_path),
        "artifact_dir": str(session.artifact_dir),
        "attempt": session.attempt,
        "turn_count": session.turn_count,
        "started_at": session.started_at,
        "last_event_at": session.last_event_at,
        "terminal_state": session.terminal_state.value if session.terminal_state else None,
    }


def _extract_permission_denials_count(worker: WorkerState) -> int:
    """Pull the count of denied tool calls from the worker's last event (#45).

    The Claude SDK surfaces ``permission_denials`` on every
    ``ResultMessage``; the provider lands the list verbatim in the
    ``turn_completed`` / ``turn_failed`` payload. Returns the length of
    that list (0 if the field is missing or the last event isn't a
    terminal turn event).

    Defensive on payload shape: a future SDK that switches to a dict-
    keyed-by-tool-name representation would surface as ``len(dict)``;
    a malformed value (string, None) returns 0 rather than raising.
    A non-zero count is logged at WARNING so an operator running
    ``--log-level info`` (the default) sees the incomplete-success
    signal even without parsing ``terminal.json``.
    """
    event = worker.last_event
    if event is None:
        return 0
    payload = event.payload or {}
    raw = payload.get("permission_denials")
    if raw is None:
        return 0
    try:
        count = len(raw)
    except TypeError:
        return 0
    if count > 0:
        _LOG.warning(
            "permission_denials=%d on terminal event for %s — "
            "Claude was denied tool calls under permission_mode; "
            "the run may have completed without taking the requested action. "
            "See docs/m3-runbook.md for the unattended permission contract.",
            count,
            worker.issue.identifier,
        )
    return count


def _maybe_log_task_outcome(worker: WorkerState, result: DetectorResult) -> None:
    """Emit operator-visible WARNING for non-completed task outcomes (SPEC §17.7).

    Generalizes the #45 ``permission_denials`` warning to every M5.1
    incomplete/blocked/retryable outcome. Operators running at the
    default ``--log-level info`` see misleading-success cases without
    parsing artifacts.
    """
    if result.task_outcome in {OUTCOME_COMPLETED_WITH_PR, OUTCOME_COMPLETED_NO_PR_DECLARED}:
        return
    _LOG.warning(
        "task_outcome=%s for %s (decided_by=%s) — see terminal.json and "
        "docs/terminal-outcomes.md for diagnosis.",
        result.task_outcome,
        worker.issue.identifier,
        result.outcome_decided_by,
    )


def _comment_blocked_outcome(
    tracker: TrackerProtocol,
    *,
    worker: WorkerState,
    detector_result: DetectorResult,
    block_reason: str,
    config: WorkflowConfig,
) -> None:
    """Surface blocked task outcomes on the issue, not just in artifacts.

    The comment is best-effort: failing to write it must not prevent the
    core state transition (`mark_issue_blocked`) from running.
    """

    body = _format_blocked_outcome_comment(
        worker=worker,
        detector_result=detector_result,
        block_reason=block_reason,
    )
    body = redact_text(
        body,
        redact_keys=config.logging.redact_keys,
        extra_secrets=(config.tracker.token,),
    )
    try:
        tracker.create_or_update_progress_comment(worker.issue, body)
    except Exception as exc:  # noqa: BLE001 - tracker failures must not mask outcome
        _LOG.warning(
            "blocked outcome comment failed for %s: %s",
            worker.issue.identifier,
            exc,
        )


def _format_blocked_outcome_comment(
    *,
    worker: WorkerState,
    detector_result: DetectorResult,
    block_reason: str,
) -> str:
    terminal_state = worker.terminal_state.value if worker.terminal_state else "ended"
    lines = [
        "<!-- symphony:blocked-outcome -->",
        (
            "Symphony blocked this issue after an unattended run did not produce "
            "verifiable completion evidence."
        ),
        "",
        f"- task_outcome: `{detector_result.task_outcome}`",
        f"- terminal_state: `{terminal_state}`",
        f"- reason: `{block_reason}`",
        f"- attempt: `{worker.session.attempt}`",
        f"- artifacts: `{worker.artifacts.root}`",
    ]
    evidence = _format_task_evidence(detector_result.task_evidence)
    if evidence:
        lines.extend(["", "Evidence:", *evidence])
    if detector_result.no_pr_reason:
        lines.extend(
            [
                "",
                f"No-PR reason: {detector_result.no_pr_reason}",
            ]
        )
    lines.extend(
        [
            "",
            (
                "Required operator action: inspect the artifacts/logs, adjust "
                "permissions or the issue instructions, then remove the blocked "
                "label when it is safe to retry."
            ),
        ]
    )
    return "\n".join(lines)


def _format_task_evidence(entries: list[dict[str, Any]]) -> list[str]:
    rows: list[str] = []
    for entry in entries:
        kind = entry.get("type")
        if kind == "permission_denied":
            tool_names = entry.get("tool_names") or []
            tools = ", ".join(str(name) for name in tool_names) if tool_names else "unknown"
            rows.append(
                "- permission_denied: "
                f"denials_count={entry.get('denials_count', 0)}, tool_names={tools}"
            )
        elif kind == "no_pr_declared":
            rows.append(f"- no_pr_declared: {entry.get('reason', '')}")
        elif kind == "branch_pushed":
            rows.append(
                f"- branch_pushed: {entry.get('name', '')} @ {entry.get('head_sha', '')}"
            )
        elif kind == "diff_in_workspace":
            rows.append(
                "- diff_in_workspace: "
                f"files_changed={entry.get('files_changed', 0)}, "
                f"lines_added={entry.get('lines_added', 0)}, "
                f"lines_removed={entry.get('lines_removed', 0)}"
            )
        elif kind == "pr_linked":
            rows.append(
                f"- pr_linked: #{entry.get('number', '')} {entry.get('url', '')}"
            )
        elif kind:
            rows.append(f"- {kind}: {json.dumps(entry, sort_keys=True)}")
    return rows


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _stamp_record_terminal(path: Path, record: SessionRecord, terminal: Terminal) -> None:
    """Patch the persisted session record's ``terminal_state`` in place.

    Used by ``Orchestrator.recover()`` so that an issue once decided
    (released / blocked / discarded) is not re-processed on the next
    daemon restart. Falls back to writing the full record snapshot if
    the file is missing — recovery has already loaded the in-memory
    copy, so we have everything we need.

    Atomic-ish: writes to ``<path>.tmp`` then renames, mirroring
    :func:`symphony.provider.claude_code._persist_session`.
    """
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        existing = {
            "session_id": record.session_id,
            "provider": record.provider,
            "issue_identifier": record.issue_identifier,
            "issue_number": record.issue_number,
            "workspace_path": str(record.workspace_path),
            "artifact_dir": str(record.artifact_dir),
            "attempt": record.attempt,
            "started_at": record.started_at.isoformat() if record.started_at else None,
            "previous_provider_session_ids": list(record.previous_provider_session_ids),
            "session_store": str(record.session_store) if record.session_store else None,
        }
    existing["terminal_state"] = terminal.value
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(existing, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def _issue_from_session_record(record: SessionRecord) -> Issue | None:
    """Build the minimal Issue shell needed for linked-PR lookup.

    ``SessionRecord.issue_identifier`` is authoritative metadata written
    at dispatch time. Do not derive this from the workspace directory
    name: workspace keys are sanitized and intentionally one-way.
    """
    ident = record.issue_identifier
    if "#" not in ident or "/" not in ident:
        return None
    owner_repo, number_text = ident.rsplit("#", 1)
    owner, repo = owner_repo.split("/", 1)
    if not owner or not repo:
        return None
    try:
        number = int(number_text)
    except ValueError:
        return None
    if number != record.issue_number:
        number = record.issue_number
    return Issue(
        id=ident,
        number=number,
        identifier=ident,
        owner=owner,
        repo=repo,
        title="",
        body="",
        state="open",
        url=f"https://github.com/{owner}/{repo}/issues/{number}",
    )


def _worker_config(worker: WorkerState, fallback: WorkflowConfig) -> WorkflowConfig:
    return worker.config if worker.config is not None else fallback


# -- turn_failed classification (#30) ---------------------------------------


# Subtype values that mean "do not retry — operator must intervene". Matches
# SPEC §9.3 ``blocked_label`` semantics. Conservative list: only auth/
# permission/structural-config errors. Everything else (transient API errors,
# rate limits, timeouts, infrastructure blips) defaults to retryable per
# SPEC §16.
_NON_RETRYABLE_TURN_FAILED_SUBTYPES: frozenset[str] = frozenset(
    {
        "auth_failed",
        "authentication_failed",
        "unauthorized",
        "permission_denied",
        "forbidden",
        "invalid_workflow",
        "invalid_config",
        "unrecoverable",
        "quota_exceeded",
        "model_unavailable",  # operator must pick a different model
    }
)


def _classify_turn_failed(event: AgentEvent | None) -> tuple[bool, str]:
    """Decide whether a ``turn_failed`` event is retryable, with reason text.

    Default: retryable. Only the subtypes in
    :data:`_NON_RETRYABLE_TURN_FAILED_SUBTYPES` (or substring matches for the
    most common auth/permission keywords) flip to non-retryable. The reason
    string lands in ``terminal.json`` and the claim-comment trail.

    Defensive on a None / wrong-shape event so a missing ``last_event`` can
    never crash the worker — falls back to retryable with a generic reason.
    """
    if event is None or event.event != "turn_failed":
        return True, "turn_failed"
    payload = event.payload or {}
    subtype = (payload.get("subtype") or "").lower().strip()
    error = payload.get("error") or payload.get("result") or ""
    if subtype in _NON_RETRYABLE_TURN_FAILED_SUBTYPES:
        return False, f"non-retryable turn_failed: {subtype}"
    # Substring fallback for SDK-side strings we haven't seen yet.
    for marker in ("auth_failed", "permission_denied", "forbidden", "unauthorized"):
        if marker in subtype:
            return False, f"non-retryable turn_failed: {subtype}"
    return True, str(error)[:200] if error else f"turn_failed: {subtype or 'unknown'}"
