"""Shared helpers for provider contract tests (issue #12).

The contract suite parametrizes the same protocol-level assertions over
:class:`~symphony.provider.fake.FakeProvider` and the real
:class:`~symphony.provider.claude_code.ClaudeCodeProvider`. To do that
we need a way to express a *scripted scenario* once and have either
provider replay it.

Symphony's :class:`~symphony.events.AgentEvent` is the canonical event
shape (the orchestrator and artifact writer only ever see normalized
events). Fixture files under ``tests/fixtures/provider_events/`` are
JSONL where each line is ``{"event": <name>, "payload": <dict>}`` —
i.e. the AgentEvent body without the envelope (timestamp, session_id,
provider, etc., all of which are stamped by the provider at emit time).

A special ``__raise__`` line aborts the scripted stream by raising the
named provider exception — used to script crash-mid-stream scenarios.

Two factories translate a loaded scenario into a provider preconfigured
to replay it:

- :func:`make_fake_provider_for` — feeds the scenario into a
  :class:`~symphony.provider.fake.FakeTurnScript` (1:1 mapping).
- :func:`make_claude_provider_for` — translates each abstract event
  into a hand-crafted fake SDK message and wires up an injected client
  factory; uses the same fake SDK shapes as
  ``tests/test_claude_provider.py``.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from symphony.config import ClaudeConfig
from symphony.provider import ClaudeCodeProvider
from symphony.provider.base import (
    ProviderError,
    ProviderRetryableError,
)
from symphony.provider.fake import FakeProvider, FakeTurnScript

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "provider_events"

# The set of provider-emitted terminal events (matches
# symphony.events.TERMINAL_TURN_EVENTS but duplicated here so contract
# tests don't reach into private orchestrator constants).
TERMINAL_EVENTS = frozenset({"turn_completed", "turn_failed", "turn_cancelled"})

# Required fields on a normalized AgentEvent envelope per SPEC §17.
REQUIRED_EVENT_FIELDS = (
    "event",
    "timestamp",
    "session_id",
    "provider",
    "issue_identifier",
    "attempt",
    "payload",
)

# Required fields on a SessionRecord per SPEC §5.4.
REQUIRED_SESSION_FIELDS = (
    "session_id",
    "provider",
    "issue_identifier",
    "issue_number",
    "workspace_path",
    "artifact_dir",
    "started_at",
    "attempt",
    "provider_session_id",
    "transcript_path",
    "turn_count",
    "last_event_at",
    "terminal_state",
    "previous_provider_session_ids",
)


# -- Scenario shape -----------------------------------------------------------


@dataclass
class ScenarioStep:
    event: str
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class ScenarioRaise:
    """``__raise__`` step — abort the stream with a typed provider error."""

    error: str  # exception class name
    message: str = ""


@dataclass
class Scenario:
    name: str
    steps: list[ScenarioStep] = field(default_factory=list)
    raise_at_end: ScenarioRaise | None = None

    @property
    def terminal_event(self) -> str | None:
        for s in reversed(self.steps):
            if s.event in TERMINAL_EVENTS:
                return s.event
        return None


def load_scenario(name: str) -> Scenario:
    """Load a JSONL scenario file from ``tests/fixtures/provider_events/``."""
    path = FIXTURE_DIR / f"{name}.jsonl"
    steps: list[ScenarioStep] = []
    raise_at_end: ScenarioRaise | None = None
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        record = json.loads(line)
        event = record.get("event")
        payload = record.get("payload") or {}
        if event == "__raise__":
            raise_at_end = ScenarioRaise(
                error=str(payload.get("error", "ProviderError")),
                message=str(payload.get("message", "scripted scenario raise")),
            )
            continue
        steps.append(ScenarioStep(event=str(event), payload=dict(payload)))
    return Scenario(name=name, steps=steps, raise_at_end=raise_at_end)


def list_scenarios() -> list[str]:
    return sorted(p.stem for p in FIXTURE_DIR.glob("*.jsonl"))


# -- Provider factories -------------------------------------------------------


# A ProviderBuilder accepts an optional Scenario and returns a
# ``(provider, claude_config_factory)`` tuple. Tests use the config
# factory to materialize a per-test ClaudeConfig under tmp_path. The
# builder is parametrized so the same contract test runs against both
# providers without duplicating the setup boilerplate.
ProviderBuilder = Callable[
    [Scenario | None],
    tuple[Any, Callable[[Path], ClaudeConfig]],
]


def make_fake_provider_for(
    scenario: Scenario | None,
) -> tuple[FakeProvider, Callable[[Path], ClaudeConfig]]:
    """Build a :class:`FakeProvider` whose default script replays ``scenario``."""
    if scenario is None:
        prov = FakeProvider()
    else:
        events = [(s.event, dict(s.payload)) for s in scenario.steps]
        raise_kwargs: dict[str, Any] = {}
        if scenario.raise_at_end is not None:
            raise_kwargs = {
                "raise_after": len(events),
                "raise_with": _resolve_error_class(scenario.raise_at_end.error),
                "raise_message": scenario.raise_at_end.message,
            }
        script = FakeTurnScript(events=events, **raise_kwargs)
        prov = FakeProvider(default_script=script)

    def cfg_factory(tmp_path: Path) -> ClaudeConfig:
        return ClaudeConfig(
            model="fake-model",
            permission_mode="acceptEdits",
            session_store=tmp_path / "sessions",
            transcript_store=tmp_path / "transcripts",
            artifact_store=tmp_path / "artifacts",
        )

    return prov, cfg_factory


def make_claude_provider_for(
    scenario: Scenario | None,
) -> tuple[ClaudeCodeProvider, Callable[[Path], ClaudeConfig]]:
    """Build a :class:`ClaudeCodeProvider` with an injected fake SDK client.

    The injected client replays ``scenario`` by emitting hand-crafted
    fake SDK messages from ``receive_response``. Mapping is documented
    in :func:`_scenario_to_sdk_messages`.
    """
    sdk_messages = _scenario_to_sdk_messages(scenario) if scenario else []
    raise_after = len(sdk_messages) if scenario and scenario.raise_at_end else None
    raise_class = (
        _resolve_error_class(scenario.raise_at_end.error)
        if (scenario and scenario.raise_at_end)
        else None
    )

    clients: list[_ScriptedSDKClient] = []

    def factory(options: dict[str, Any]) -> _ScriptedSDKClient:
        client = _ScriptedSDKClient(
            options=options,
            messages=list(sdk_messages),
            raise_after=raise_after,
            raise_class=raise_class,
        )
        clients.append(client)
        return client

    # Fixed-shape session id factory so tests can assert reuse.
    counter = {"i": 0}

    def session_id_factory() -> str:
        counter["i"] += 1
        return f"sym-contract-{counter['i']:03d}"

    provider = ClaudeCodeProvider(
        client_factory=factory,
        session_id_factory=session_id_factory,
    )
    # Surface the per-call client list for tests that want to inspect
    # SDK calls — attach lazily so the type stays ClaudeCodeProvider.
    provider._test_clients = clients  # type: ignore[attr-defined]

    def cfg_factory(tmp_path: Path) -> ClaudeConfig:
        return ClaudeConfig(
            model="claude-fake",
            permission_mode="acceptEdits",
            session_store=tmp_path / "sessions",
            transcript_store=tmp_path / "transcripts",
            artifact_store=tmp_path / "artifacts",
        )

    return provider, cfg_factory


# -- Internal: SDK shim shapes (mirror tests/test_claude_provider.py) --------
#
# Class names MUST match the real SDK class names because the provider's
# normalizer matches on ``type(raw).__name__``. We're not subclassing the
# SDK types — these are standalone test fakes — so the name is the only
# tie-breaker. Underscore-prefixed names would silently route to the
# "unknown message" / "malformed" branch and break every contract test.


@dataclass
class TextBlock:
    text: str


@dataclass
class ToolUseBlock:
    id: str
    name: str
    input: dict[str, Any]


@dataclass
class ToolResultBlock:
    tool_use_id: str
    is_error: bool = False
    content: str = ""


@dataclass
class AssistantMessage:
    content: list[Any]
    session_id: str | None = None
    model: str | None = None
    message_id: str | None = None
    stop_reason: str | None = None
    usage: dict[str, int] | None = None


@dataclass
class UserMessage:
    content: list[Any]
    tool_use_result: Any = None


@dataclass
class SystemMessage:
    subtype: str
    data: dict[str, Any]


@dataclass
class ResultMessage:
    is_error: bool = False
    subtype: str = "success"
    duration_ms: int = 1
    duration_api_ms: int = 1
    num_turns: int = 1
    total_cost_usd: float = 0.0
    usage: dict[str, int] | None = None
    result: str | None = None
    structured_output: Any = None
    permission_denials: list[Any] = field(default_factory=list)
    session_id: str | None = None


@dataclass
class RateLimitEvent:
    retry_after: int = 30


# Errors that mimic the SDK names so _wrap_sdk_error in the provider
# routes them to ProviderRetryableError.
class _CLIConnectionError(Exception):
    """Class name matches the SDK's CLIConnectionError so the provider's
    error wrapper routes it to ProviderRetryableError. Underscore prefix
    is fine here because the wrapper matches ``type(exc).__name__`` —
    which we override below at the raise site."""


# Force the public SDK name on the wire so the wrapper matches.
_CLIConnectionError.__name__ = "CLIConnectionError"


class _ScriptedSDKClient:
    """Minimal fake of ``ClaudeSDKClient`` for the contract suite.

    Records every call for assertions and yields prebuilt messages from
    ``receive_response``. Optionally raises mid-stream after ``raise_after``
    messages have been yielded — matches ``__raise__`` step semantics.
    """

    def __init__(
        self,
        *,
        options: dict[str, Any],
        messages: list[Any],
        raise_after: int | None = None,
        raise_class: type[BaseException] | None = None,
    ) -> None:
        self.options = options
        self._messages = messages
        self._raise_after = raise_after
        self._raise_class = raise_class
        self.calls: list[tuple[str, Any]] = []
        self.connected = False
        self.disconnected = False

    async def connect(self, prompt: Any = None) -> None:
        self.calls.append(("connect", prompt))
        self.connected = True

    async def query(self, prompt: str, session_id: str = "default") -> None:
        self.calls.append(("query", (prompt, session_id)))

    async def receive_response(self) -> AsyncIterator[Any]:
        self.calls.append(("receive_response", None))
        for i, msg in enumerate(self._messages):
            yield msg
            if self._raise_after is not None and i + 1 >= self._raise_after:
                cls = self._raise_class or RuntimeError
                if cls is ProviderRetryableError:
                    # Simulate the SDK side by raising a CLIConnectionError
                    # so the provider's _wrap_sdk_error categorizes it as
                    # retryable.
                    raise _CLIConnectionError("scripted contract crash")
                raise cls("scripted contract crash")

    async def interrupt(self) -> None:
        self.calls.append(("interrupt", None))

    async def disconnect(self) -> None:
        self.calls.append(("disconnect", None))
        self.disconnected = True


def _scenario_to_sdk_messages(scenario: Scenario) -> list[Any]:
    """Translate Symphony abstract events back into fake SDK messages.

    The mapping is the inverse of the provider's ``_normalize_message``.
    Adjacent ``message_delta`` / ``tool_started`` events that share a
    provider AssistantMessage in real life can be batched here, but the
    contract tests don't depend on that — emitting one
    ``_AssistantMessage`` per event keeps the translation lossless.

    Each ``_AssistantMessage`` carries ``session_id="claude-contract-pid"``
    so the provider's first-event capture path lands a stable id; tests
    can assert it directly. The synthesized ``message_completed`` event
    that the provider would emit at the close of every AssistantMessage
    is implicit in the SDK message boundary.
    """
    out: list[Any] = []
    pid = "claude-contract-pid"
    saw_first = False
    for step in scenario.steps:
        first_session_id = pid if not saw_first else None
        saw_first = True
        e = step.event
        p = step.payload
        if e == "message_delta":
            out.append(
                AssistantMessage(
                    content=[TextBlock(text=str(p.get("text", "")))],
                    session_id=first_session_id,
                    model="claude-fake",
                    stop_reason="end_turn",
                )
            )
        elif e == "message_completed":
            # Already implied by the AssistantMessage close. Skip.
            continue
        elif e == "tool_started":
            out.append(
                AssistantMessage(
                    content=[
                        ToolUseBlock(
                            id=str(p.get("tool_use_id", "tu_x")),
                            name=str(p.get("tool_name", "shell")),
                            input=dict(p.get("input") or {}),
                        )
                    ],
                    session_id=first_session_id,
                    model="claude-fake",
                )
            )
        elif e == "tool_completed":
            out.append(
                UserMessage(
                    content=[
                        ToolResultBlock(
                            tool_use_id=str(p.get("tool_use_id", "tu_x")),
                            is_error=bool(p.get("is_error", False)),
                            content=str(p.get("content", "")),
                        )
                    ]
                )
            )
        elif e == "permission_requested":
            out.append(
                SystemMessage(
                    subtype=str(p.get("subtype", "permission_request")),
                    data=dict(p.get("data") or {}),
                )
            )
        elif e == "permission_resolved":
            out.append(
                SystemMessage(
                    subtype=str(p.get("subtype", "permission_decision")),
                    data=dict(p.get("data") or {}),
                )
            )
        elif e == "usage":
            out.append(
                SystemMessage(
                    subtype=str(p.get("subtype", "usage_report")),
                    data=dict(p.get("data") or {}),
                )
            )
        elif e == "heartbeat":
            kind = str(p.get("kind", ""))
            if kind == "rate_limit":
                retry_after = int(p.get("data", {}).get("retry_after", 30))
                out.append(RateLimitEvent(retry_after=retry_after))
            else:
                out.append(
                    SystemMessage(
                        subtype=str(p.get("subtype", "")),
                        data=dict(p.get("data") or {}),
                    )
                )
        elif e == "malformed":
            # Trigger the provider's "unknown message class" branch by
            # passing a class it doesn't recognize.
            out.append(_UnknownThing(payload=p))
        elif e == "turn_completed":
            out.append(ResultMessage(is_error=False, subtype="success", **_result_kwargs(p)))
        elif e == "turn_failed":
            out.append(
                ResultMessage(
                    is_error=True,
                    subtype=str(p.get("subtype", "error")),
                    result=str(p.get("error", "")),
                    **_result_kwargs(p, exclude={"subtype", "error"}),
                )
            )
        elif e == "turn_cancelled":
            out.append(
                ResultMessage(
                    is_error=True,
                    subtype="cancelled",
                    **_result_kwargs(p, exclude={"subtype"}),
                )
            )
        else:
            # Anything else shouldn't appear in fixtures; surface loudly.
            raise ValueError(f"contract scenario uses unknown event type: {e!r}")
    return out


@dataclass
class _UnknownThing:
    payload: dict[str, Any]


def _result_kwargs(payload: dict[str, Any], exclude: Iterable[str] = ()) -> dict[str, Any]:
    """Carry over the well-known ResultMessage fields from a payload."""
    skip = set(exclude)
    out: dict[str, Any] = {}
    for k in ("duration_ms", "duration_api_ms", "num_turns", "result"):
        if k in skip:
            continue
        if k in payload:
            out[k] = payload[k]
    return out


def _resolve_error_class(name: str) -> type[BaseException]:
    if name == "ProviderRetryableError":
        return ProviderRetryableError
    if name == "ProviderError":
        return ProviderError
    return RuntimeError


# -- Schema validators --------------------------------------------------------


def assert_event_envelope_shape(event: Any, *, provider_name: str) -> None:
    """Assert the AgentEvent has every required envelope field per SPEC §17.

    The contract is structural: implementations are free to add extra
    fields, but the orchestrator + artifact writer depend on the
    canonical set being present and well-typed.
    """
    for fld in REQUIRED_EVENT_FIELDS:
        assert hasattr(event, fld), f"event missing required field {fld!r}: {event!r}"
    assert event.provider == provider_name
    assert isinstance(event.event, str) and event.event
    assert isinstance(event.session_id, str) and event.session_id
    assert isinstance(event.attempt, int) and event.attempt >= 1
    assert isinstance(event.payload, dict)


def assert_session_record_shape(record: Any) -> None:
    for fld in REQUIRED_SESSION_FIELDS:
        assert hasattr(record, fld), f"SessionRecord missing required field {fld!r}: {record!r}"
    assert isinstance(record.session_id, str) and record.session_id
    assert isinstance(record.attempt, int) and record.attempt >= 1
    assert isinstance(record.turn_count, int) and record.turn_count >= 0
    assert isinstance(record.previous_provider_session_ids, list)
