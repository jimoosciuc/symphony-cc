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
import logging
import uuid
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from symphony.artifacts import ArtifactWriter
from symphony.config import WorkflowConfig
from symphony.events import TERMINAL_TURN_EVENTS, AgentEvent
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
from symphony.retry import RetryState, next_backoff_ms
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
    """

    issue: Issue
    workspace: Workspace
    session: SessionRecord
    artifacts: ArtifactWriter
    turn_count: int = 0
    terminal_state: Terminal | None = None
    last_event: AgentEvent | None = None
    error: str | None = None


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
    ) -> None:
        self.config = config
        self.tracker = tracker
        self.provider = provider
        self.workspaces = workspace_manager
        self.continuation_policy = continuation_policy or _default_continuation_policy
        self.run_id = run_id or f"run-{uuid.uuid4().hex[:8]}"
        self._clock = clock or _now_utc

        # Mutable state.
        self.active: dict[str, WorkerState] = {}
        self.retry_states: dict[str, RetryState] = {}

    # -- Public API ---------------------------------------------------------

    async def run_once(self) -> TickResult:
        result = TickResult()

        # 1. Reconcile currently-active workers against fresh issue state.
        await self._reconcile(result)

        # 2. Fetch candidate issues + dispatch up to the concurrency limit.
        candidates = self.tracker.fetch_candidate_issues()
        await self._dispatch(candidates, result)

        return result

    async def run_forever(self) -> None:  # pragma: no cover - simple loop
        """Convenience scheduler. Tests use ``run_once`` directly."""
        interval = max(0.001, self.config.polling.interval_ms / 1000)
        while True:
            await self.run_once()
            await asyncio.sleep(interval)

    # -- Reconciliation -----------------------------------------------------

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
            became_ineligible = (
                issue.state != "open"
                or self.config.tracker.exclude_labels
                and any(lbl in issue.labels for lbl in self.config.tracker.exclude_labels)
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
        for issue in candidates:
            if slots_open == 0:
                break
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

            try:
                worker = await self._start_worker(issue, retry=retry)
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

            # Inline run-the-worker. The orchestrator could run workers
            # concurrently as asyncio tasks; for the M1 milestone keeping
            # them serialized inside one tick keeps tests deterministic
            # without losing the concurrency-cap semantic.
            await self._run_worker(worker, result, claim)

    async def _start_worker(
        self,
        issue: Issue,
        *,
        retry: RetryState | None,
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
                "run_id": self.run_id,
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
        session.artifact_dir = artifacts.root

        artifacts.write_json("session.json", _session_snapshot(session))
        return WorkerState(issue=issue, workspace=workspace, session=session, artifacts=artifacts)

    async def _run_worker(
        self,
        worker: WorkerState,
        result: TickResult,
        claim: ClaimResult,
    ) -> None:
        del claim  # claim is informational; release happens regardless of outcome
        max_turns = self.config.agent.max_turns
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
                if terminal in {"turn_failed", "turn_cancelled"}:
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
            self.tracker.release_issue(
                worker.issue,
                worker.terminal_state.value if worker.terminal_state else "ended",
            )
            worker.artifacts.write_json(
                "terminal.json",
                {
                    "terminal_state": (
                        worker.terminal_state.value if worker.terminal_state else "ended"
                    ),
                    "last_event_at": (worker.last_event.timestamp if worker.last_event else None),
                    "provider_session_id": worker.session.provider_session_id,
                    "error": worker.error,
                    "turn_count": worker.turn_count,
                },
            )
            self.active.pop(worker.issue.identifier, None)
            result.finished.append(worker.issue.identifier)
            if worker.terminal_state == Terminal.COMPLETED:
                rs = self.retry_states.pop(worker.issue.identifier, None)
                if rs is not None:
                    rs.record_success(now=self._clock())

    async def _run_one_turn(self, worker: WorkerState, message: str) -> str:
        """Drive one send_input turn to its terminal event.

        Returns the terminal event name (``turn_completed`` /
        ``turn_failed`` / ``turn_cancelled``) or ``"no_terminal"`` if the
        stream ended without one (treated as a crash by the caller).

        ``turn_completed`` does NOT set ``worker.terminal_state`` because
        a successful turn does not end the session — the orchestrator may
        send another continuation prompt.
        """
        terminal = "no_terminal"
        async for event in self.provider.send_input(worker.session, message):
            self._record_event(worker, event)
            if event.event in TERMINAL_TURN_EVENTS:
                terminal = event.event
                if event.event == "turn_failed":
                    worker.terminal_state = Terminal.FAILED
                elif event.event == "turn_cancelled":
                    worker.terminal_state = Terminal.CANCELLED
                # turn_completed: leave terminal_state untouched.
                break
        return terminal

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
        worker.session.last_event_at = event.timestamp
        worker.artifacts.append_event(event)


# -- Helpers ------------------------------------------------------------------


def _default_continuation_policy(worker: WorkerState) -> str | None:
    """Default: send one prompt, then stop.

    Tests provide their own policies for multi-turn behavior.
    """
    if worker.turn_count == 0:
        return f"first prompt for {worker.issue.identifier}"
    return None


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


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


# Help static analysis: Awaitable is used implicitly by Callable type hints.
_ = Awaitable
