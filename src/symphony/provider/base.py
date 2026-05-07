"""Agent provider boundary.

The orchestrator interacts with providers through the
:class:`AgentProviderProtocol` only. Provider implementations (fake,
Claude Code in #9) MUST conform to ``SPEC.md`` §10 — see
``docs/claude-provider.md`` for the rationale behind the
no-stream-from-start_session split.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Protocol, runtime_checkable

from symphony.config import ClaudeConfig
from symphony.events import AgentEvent
from symphony.models import Issue

# -- Errors -------------------------------------------------------------------


class ProviderError(RuntimeError):
    """Generic provider failure surface.

    Sub-classes signal whether the orchestrator should retry. The default
    is non-retryable; provider implementations raise
    :class:`ProviderRetryableError` for transient issues.
    """


class ProviderRetryableError(ProviderError):
    """Provider failed in a way the orchestrator should retry per RetryConfig."""


class ProviderRestoreError(ProviderError):
    """``restore()`` could not stand the SDK back up.

    Per ``docs/claude-provider.md`` §2.1 / §5.3 this is a typed exception
    (not a turn event) because no generator is in flight when restore
    fails. The orchestrator catches it and routes to
    ``claude.retry_resume_policy``.
    """


# -- Records ------------------------------------------------------------------


class Terminal(str, Enum):
    """Reason a session reached its terminal state."""

    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    CRASHED = "crashed"


@dataclass(slots=True)
class SessionRecord:
    """Per-session state shared between provider and orchestrator.

    Mutable on purpose: provider implementations bump ``turn_count``,
    populate ``provider_session_id`` after the first send_input, and
    stamp ``terminal_state`` on close. The artifact writer snapshots a
    redacted copy to ``session.json`` after every event flush.
    """

    session_id: str
    provider: str
    issue_identifier: str
    issue_number: int
    workspace_path: Path
    artifact_dir: Path
    started_at: datetime
    attempt: int = 1
    provider_session_id: str | None = None
    transcript_path: Path | None = None
    turn_count: int = 0
    last_event_at: datetime | None = None
    terminal_state: Terminal | None = None
    previous_provider_session_ids: list[str] = field(default_factory=list)


# -- Protocol -----------------------------------------------------------------


@runtime_checkable
class AgentProviderProtocol(Protocol):
    """Per ``SPEC.md`` §10 and ``docs/claude-provider.md`` §2.1.

    All methods are async. ``start_session`` and ``restore`` return a
    :class:`SessionRecord` and emit no events; ``send_input`` is the only
    method that yields :class:`~symphony.events.AgentEvent`s.
    """

    name: str  # canonical provider name, e.g. "claude_code" or "fake"

    async def start_session(
        self,
        issue: Issue,
        workspace_path: Path,
        config: ClaudeConfig,
    ) -> SessionRecord: ...

    def send_input(
        self,
        session: SessionRecord,
        message: str,
    ) -> AsyncIterator[AgentEvent]: ...

    async def interrupt(self, session: SessionRecord) -> AgentEvent: ...

    async def cancel(self, session: SessionRecord) -> AgentEvent: ...

    async def close(self, session: SessionRecord) -> None: ...

    async def restore(self, session_record: SessionRecord) -> SessionRecord: ...
