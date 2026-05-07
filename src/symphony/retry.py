"""Retry state and backoff calculation.

The orchestrator uses :class:`RetryState` to record per-issue attempt
history and :func:`next_backoff_ms` to compute when the next attempt may
run. The math is centralized here so tests can pin it independently from
the orchestrator's scheduling loop.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from symphony.config import RetryConfig


@dataclass(slots=True)
class RetryState:
    """Mutable per-issue retry bookkeeping.

    Held by the orchestrator inside its in-memory state; persisted to disk
    only via the artifact records (SPEC §16 restart recovery is built from
    artifacts + tracker labels, not a separate retry DB).
    """

    issue_identifier: str
    attempts: int = 0
    last_error: str | None = None
    last_attempt_at: datetime | None = None
    next_attempt_at: datetime | None = None
    history: list[str] = field(default_factory=list)

    def record_failure(self, error: str, *, now: datetime, backoff_ms: int) -> None:
        self.attempts += 1
        self.last_error = error
        self.last_attempt_at = now
        self.next_attempt_at = now + timedelta(milliseconds=backoff_ms)
        self.history.append(f"{now.isoformat()}: {error}")

    def record_success(self, *, now: datetime) -> None:
        self.attempts += 1
        self.last_error = None
        self.last_attempt_at = now
        self.next_attempt_at = None
        self.history.append(f"{now.isoformat()}: success")

    def should_run(self, *, now: datetime) -> bool:
        """Return True if the orchestrator may dispatch this issue now.

        ``next_attempt_at is None`` means either no failure has happened
        yet or the issue has succeeded; either way it is eligible.
        """
        if self.next_attempt_at is None:
            return True
        return now >= self.next_attempt_at


def next_backoff_ms(config: RetryConfig, *, attempt: int) -> int:
    """Exponential backoff with cap.

    ``attempt`` is the 1-indexed retry number (1 = the first retry, after
    one failure has occurred). The formula is
    ``initial * multiplier ** (attempt - 1)`` clamped to ``max_backoff_ms``.
    """
    if attempt < 1:
        raise ValueError("attempt must be >= 1")
    raw = config.initial_backoff_ms * (config.multiplier ** (attempt - 1))
    return int(min(raw, config.max_backoff_ms))


def now_utc() -> datetime:
    """Indirection point for test-time monkey-patching."""
    return datetime.now(timezone.utc)
