"""Normalized agent events.

Every event that crosses the provider boundary is an :class:`AgentEvent`
with the §5.5 envelope from ``SPEC.md``. Provider implementations
translate vendor-specific message types into one of the names in
:data:`EVENT_NAMES`; the orchestrator and observability layers depend
only on this shape.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

EVENT_NAMES: frozenset[str] = frozenset(
    {
        "session_started",
        "session_restored",
        "heartbeat",
        "message_delta",
        "message_completed",
        "tool_started",
        "tool_completed",
        "permission_requested",
        "permission_resolved",
        "usage",
        "turn_completed",
        "turn_failed",
        "turn_cancelled",
        "session_closed",
        "malformed",
    }
)

TERMINAL_TURN_EVENTS: frozenset[str] = frozenset(
    {"turn_completed", "turn_failed", "turn_cancelled"}
)
"""Events that mark the end of a single send_input turn.

The orchestrator's send_input loop terminates when one of these arrives;
they are the only events for which the provider guarantees no follow-up
within the same turn.
"""


@dataclass(frozen=True, slots=True)
class AgentEvent:
    """A single normalized event from an agent provider session.

    See ``SPEC.md`` §5.5. ``provider_session_id`` is allowed to be
    ``None`` only on the synthesized ``session_started`` event itself
    (which carries the just-discovered id in its payload); every event
    after that within an attempt has it populated.
    """

    event: str
    timestamp: datetime
    session_id: str
    provider: str
    issue_identifier: str
    attempt: int
    payload: dict[str, Any] = field(default_factory=dict)
    provider_session_id: str | None = None

    def __post_init__(self) -> None:
        if self.event not in EVENT_NAMES:
            raise ValueError(f"unknown event name: {self.event!r}")


def now_utc() -> datetime:
    """Single source of truth for event timestamps. Easy to monkey-patch in tests."""
    return datetime.now(timezone.utc)
