"""Unit tests for the Claude Code provider (issue #9).

Uses a hand-written :class:`_FakeClient` that mirrors the SDK surface
(``connect``, ``query``, ``receive_response``, ``interrupt``,
``disconnect``) and yields hand-crafted message objects. No
``claude-agent-sdk`` install required at test time — the provider is
constructed with ``client_factory`` injected.

Live SDK integration tests live in ``tests/test_claude_provider_live.py``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from symphony.config import ClaudeConfig
from symphony.models import Issue
from symphony.provider import ClaudeCodeProvider
from symphony.provider.base import (
    AgentProviderProtocol,
    ProviderError,
    ProviderRestoreError,
    ProviderRetryableError,
    SessionRecord,
)

# -- Fake SDK message classes ------------------------------------------------


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


# -- Fake SDK client ---------------------------------------------------------


class _FakeClient:
    """Mirrors the subset of ClaudeSDKClient the provider uses.

    Tests pre-load ``responses`` (a list of messages to yield from
    receive_response) and inspect ``calls`` for assertions.
    """

    def __init__(
        self,
        options: dict[str, Any],
        *,
        responses: list[Any] | None = None,
        connect_raises: BaseException | None = None,
        query_raises: BaseException | None = None,
        iterate_raises: BaseException | None = None,
    ) -> None:
        self.options = options
        self._responses = list(responses or [])
        self.connect_raises = connect_raises
        self.query_raises = query_raises
        self.iterate_raises = iterate_raises
        self.calls: list[tuple[str, Any]] = []
        self.connected = False
        self.disconnected = False

    async def connect(self, prompt: Any = None) -> None:
        self.calls.append(("connect", prompt))
        if self.connect_raises is not None:
            raise self.connect_raises
        self.connected = True

    async def query(self, prompt: str, session_id: str = "default") -> None:
        self.calls.append(("query", (prompt, session_id)))
        if self.query_raises is not None:
            raise self.query_raises

    async def receive_response(self) -> AsyncIterator[Any]:
        self.calls.append(("receive_response", None))
        if self.iterate_raises is not None:
            raise self.iterate_raises
        for r in self._responses:
            yield r

    async def interrupt(self) -> None:
        self.calls.append(("interrupt", None))

    async def disconnect(self) -> None:
        self.calls.append(("disconnect", None))
        self.disconnected = True


# Errors that mimic the SDK's exception names so _wrap_sdk_error works.
class CLIConnectionError(Exception):
    pass


class ProcessError(Exception):
    pass


# -- Fixtures ---------------------------------------------------------------


def _issue(*, number: int = 1) -> Issue:
    return Issue(
        id=f"I_{number}",
        number=number,
        identifier=f"acme/proj#{number}",
        owner="acme",
        repo="proj",
        title=f"t{number}",
        body="b",
        state="open",
        url=f"https://github.com/acme/proj/issues/{number}",
    )


def _claude_config(tmp_path: Path) -> ClaudeConfig:
    return ClaudeConfig(
        model="claude-sonnet-4-5",
        permission_mode="acceptEdits",
        session_store=tmp_path / "sessions",
        transcript_store=tmp_path / "transcripts",
        artifact_store=tmp_path / "artifacts",
    )


def _make_provider(
    *,
    responses: list[Any] | None = None,
    connect_raises: BaseException | None = None,
    query_raises: BaseException | None = None,
    iterate_raises: BaseException | None = None,
    session_ids: list[str] | None = None,
) -> tuple[ClaudeCodeProvider, list[_FakeClient]]:
    """Returns the provider plus a list that captures every fake client
    the factory produces (one per start_session/restore call)."""
    clients: list[_FakeClient] = []

    def factory(options: dict[str, Any]) -> _FakeClient:
        client = _FakeClient(
            options,
            responses=responses,
            connect_raises=connect_raises,
            query_raises=query_raises,
            iterate_raises=iterate_raises,
        )
        clients.append(client)
        return client

    if session_ids is not None:
        ids = iter(session_ids)
        sf = lambda: next(ids)  # noqa: E731 - tiny test helper
    else:
        sf = None
    provider = ClaudeCodeProvider(client_factory=factory, session_id_factory=sf)
    return provider, clients


# -- Protocol conformance ----------------------------------------------------


def test_claude_code_provider_satisfies_protocol() -> None:
    provider = ClaudeCodeProvider(client_factory=lambda _o: _FakeClient(_o))
    # Protocol is structural; assert each method exists.
    for name in (
        "start_session",
        "send_input",
        "interrupt",
        "cancel",
        "close",
        "restore",
    ):
        assert callable(getattr(provider, name)), f"missing {name}"
    assert provider.name == "claude_code"


def test_provider_is_runtime_protocol_instance() -> None:
    provider = ClaudeCodeProvider(client_factory=lambda _o: _FakeClient(_o))
    assert isinstance(provider, AgentProviderProtocol)


# -- start_session: connect-only, no events ----------------------------------


async def test_start_session_connects_and_returns_record_with_no_events(
    tmp_path: Path,
) -> None:
    provider, clients = _make_provider(session_ids=["sym-0001"])
    record = await provider.start_session(_issue(), tmp_path / "ws", _claude_config(tmp_path))

    assert record.session_id == "sym-0001"
    assert record.provider == "claude_code"
    assert record.provider_session_id is None
    assert record.attempt == 1
    assert record.turn_count == 0

    # One client was created and connected; no events streamed.
    assert len(clients) == 1
    assert clients[0].connected is True
    methods = [m for m, _ in clients[0].calls]
    assert "connect" in methods
    assert "query" not in methods
    assert "receive_response" not in methods


def test_start_session_options_match_config(tmp_path: Path) -> None:
    provider, clients = _make_provider()
    cfg = _claude_config(tmp_path)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    import asyncio

    asyncio.run(provider.start_session(_issue(), workspace, cfg))
    opts = clients[0].options
    assert opts["cwd"] == str(workspace)
    assert opts["model"] == "claude-sonnet-4-5"
    assert opts["permission_mode"] == "acceptEdits"
    assert opts["fork_session"] is False
    assert opts["continue_conversation"] is False
    assert "resume" not in opts  # start_session never sets resume


async def test_start_session_connect_failure_raises_provider_error(tmp_path: Path) -> None:
    boom = RuntimeError("can't talk to CLI")
    provider, _ = _make_provider(connect_raises=boom)
    with pytest.raises(ProviderError) as excinfo:
        await provider.start_session(_issue(), tmp_path / "ws", _claude_config(tmp_path))
    assert "client.connect()" in str(excinfo.value)


# -- send_input: first call emits session_started -----------------------------


async def test_first_send_input_after_start_emits_session_started(tmp_path: Path) -> None:
    responses = [
        AssistantMessage(
            content=[TextBlock(text="hello")],
            session_id="claude-pid-abc",
            model="claude-sonnet-4-5",
            message_id="msg_1",
        ),
        ResultMessage(is_error=False, result="ok", session_id="claude-pid-abc"),
    ]
    provider, _ = _make_provider(responses=responses)
    record = await provider.start_session(_issue(), tmp_path / "ws", _claude_config(tmp_path))

    events = [ev async for ev in provider.send_input(record, "first prompt")]
    names = [e.event for e in events]
    # session_started MUST be the first event of the first send_input.
    assert names[0] == "session_started"
    assert events[0].payload["session_id"] == "claude-pid-abc"
    assert events[0].payload["model"] == "claude-sonnet-4-5"
    # Then content (message_delta + message_completed) and finally turn_completed.
    assert "message_delta" in names
    assert "message_completed" in names
    assert names[-1] == "turn_completed"
    # Every event after session_started carries the captured pid.
    for ev in events[1:]:
        assert ev.provider_session_id == "claude-pid-abc"
    # The session record was updated.
    assert record.provider_session_id == "claude-pid-abc"


async def test_continuation_send_input_does_not_re_emit_session_started(
    tmp_path: Path,
) -> None:
    """Second send_input on the same session must NOT synthesize
    session_started again."""
    responses_round1 = [
        AssistantMessage(content=[TextBlock(text="r1")], session_id="pid-1", model="m"),
        ResultMessage(is_error=False, result="r1", session_id="pid-1"),
    ]
    provider, clients = _make_provider(responses=responses_round1)
    record = await provider.start_session(_issue(), tmp_path / "ws", _claude_config(tmp_path))
    _ = [_ async for _ in provider.send_input(record, "first")]

    # Reload the fake's response list for round 2.
    clients[0]._responses = [
        AssistantMessage(content=[TextBlock(text="r2")], session_id="pid-1", model="m"),
        ResultMessage(is_error=False, result="r2", session_id="pid-1"),
    ]
    events2 = [ev async for ev in provider.send_input(record, "second")]
    names2 = [e.event for e in events2]
    assert "session_started" not in names2
    assert names2[-1] == "turn_completed"


async def test_send_input_query_failure_maps_to_provider_error(tmp_path: Path) -> None:
    provider, _ = _make_provider(query_raises=RuntimeError("boom"))
    record = await provider.start_session(_issue(), tmp_path / "ws", _claude_config(tmp_path))
    with pytest.raises(ProviderError):
        async for _ in provider.send_input(record, "x"):
            pass


async def test_cli_connection_error_during_iter_is_retryable(tmp_path: Path) -> None:
    provider, _ = _make_provider(iterate_raises=CLIConnectionError("subprocess died"))
    record = await provider.start_session(_issue(), tmp_path / "ws", _claude_config(tmp_path))
    with pytest.raises(ProviderRetryableError):
        async for _ in provider.send_input(record, "x"):
            pass


# -- restore: connect-only, no events; session_restored on next send_input ---


async def test_restore_happy_path_emits_session_restored_on_next_send_input(
    tmp_path: Path,
) -> None:
    # Pre-built session record from a prior attempt.
    record = SessionRecord(
        session_id="sym-restore",
        provider="claude_code",
        issue_identifier="acme/proj#1",
        issue_number=1,
        workspace_path=tmp_path / "ws",
        artifact_dir=tmp_path / "artifacts" / "acme_proj_1" / "1",
        started_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        attempt=1,
        provider_session_id="claude-pid-prior",
        turn_count=5,
        session_store=tmp_path / "sessions",
    )
    responses = [
        AssistantMessage(
            content=[TextBlock(text="resumed")],
            session_id="claude-pid-prior",
            model="m",
        ),
        ResultMessage(is_error=False, result="ok", session_id="claude-pid-prior"),
    ]
    provider, clients = _make_provider(responses=responses)
    restored = await provider.restore(record)
    # restore() does NOT stream events. turn_count is left to the
    # orchestrator's max-turns accounting; synthesis is gated on the
    # per-attempt saw_first_event flag inside _ProviderSessionState.
    assert restored.attempt == 2
    assert "claude-pid-prior" in restored.previous_provider_session_ids

    # The single client was constructed with resume= set.
    assert clients[0].options["resume"] == "claude-pid-prior"

    # First send_input after restore emits session_restored, NOT
    # session_started.
    events = [ev async for ev in provider.send_input(restored, "continue")]
    names = [e.event for e in events]
    assert names[0] == "session_restored"
    assert "session_started" not in names


async def test_restore_without_provider_session_id_raises(tmp_path: Path) -> None:
    record = SessionRecord(
        session_id="sym-nopid",
        provider="claude_code",
        issue_identifier="acme/proj#1",
        issue_number=1,
        workspace_path=tmp_path / "ws",
        artifact_dir=tmp_path / "artifacts" / "x" / "1",
        started_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        provider_session_id=None,
    )
    provider, _ = _make_provider()
    with pytest.raises(ProviderRestoreError):
        await provider.restore(record)


async def test_restore_connect_failure_raises_restore_error(tmp_path: Path) -> None:
    record = SessionRecord(
        session_id="sym-fail",
        provider="claude_code",
        issue_identifier="acme/proj#1",
        issue_number=1,
        workspace_path=tmp_path / "ws",
        artifact_dir=tmp_path / "artifacts" / "x" / "1",
        started_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        provider_session_id="claude-pid-prior",
    )
    provider, _ = _make_provider(connect_raises=RuntimeError("CLI store missing"))
    with pytest.raises(ProviderRestoreError):
        await provider.restore(record)


# -- Event normalization ----------------------------------------------------


async def test_tool_use_and_result_round_trip(tmp_path: Path) -> None:
    responses = [
        AssistantMessage(
            content=[
                TextBlock(text="thinking"),
                ToolUseBlock(id="tu_1", name="shell", input={"cmd": "ls"}),
            ],
            session_id="pid-1",
            model="m",
        ),
        UserMessage(content=[ToolResultBlock(tool_use_id="tu_1", content="output")]),
        ResultMessage(is_error=False, result="ok", session_id="pid-1"),
    ]
    provider, _ = _make_provider(responses=responses)
    record = await provider.start_session(_issue(), tmp_path / "ws", _claude_config(tmp_path))
    events = [ev async for ev in provider.send_input(record, "x")]
    names = [e.event for e in events]
    assert "tool_started" in names
    assert "tool_completed" in names
    started = next(e for e in events if e.event == "tool_started")
    assert started.payload["tool_name"] == "shell"
    assert started.payload["tool_use_id"] == "tu_1"
    assert started.payload["input"] == {"cmd": "ls"}
    completed = next(e for e in events if e.event == "tool_completed")
    assert completed.payload["tool_use_id"] == "tu_1"


async def test_rate_limit_event_maps_to_heartbeat_with_kind_rate_limit(
    tmp_path: Path,
) -> None:
    responses = [
        AssistantMessage(content=[TextBlock(text="...")], session_id="pid", model="m"),
        RateLimitEvent(retry_after=60),
        ResultMessage(is_error=False, result="ok", session_id="pid"),
    ]
    provider, _ = _make_provider(responses=responses)
    record = await provider.start_session(_issue(), tmp_path / "ws", _claude_config(tmp_path))
    events = [ev async for ev in provider.send_input(record, "x")]
    rate = next(
        e for e in events if e.event == "heartbeat" and e.payload.get("kind") == "rate_limit"
    )
    assert rate is not None


async def test_result_message_is_error_maps_to_turn_failed(tmp_path: Path) -> None:
    responses = [
        AssistantMessage(content=[TextBlock(text="...")], session_id="pid", model="m"),
        ResultMessage(is_error=True, subtype="api_error", result=None, session_id="pid"),
    ]
    provider, _ = _make_provider(responses=responses)
    record = await provider.start_session(_issue(), tmp_path / "ws", _claude_config(tmp_path))
    events = [ev async for ev in provider.send_input(record, "x")]
    assert events[-1].event == "turn_failed"


async def test_result_message_cancelled_subtype_maps_to_turn_cancelled(
    tmp_path: Path,
) -> None:
    responses = [
        AssistantMessage(content=[TextBlock(text="...")], session_id="pid", model="m"),
        ResultMessage(is_error=True, subtype="cancelled", session_id="pid"),
    ]
    provider, _ = _make_provider(responses=responses)
    record = await provider.start_session(_issue(), tmp_path / "ws", _claude_config(tmp_path))
    events = [ev async for ev in provider.send_input(record, "x")]
    assert events[-1].event == "turn_cancelled"


async def test_unknown_message_type_emits_malformed(tmp_path: Path) -> None:
    class _UnknownThing:
        pass

    responses = [
        AssistantMessage(content=[TextBlock(text="...")], session_id="pid", model="m"),
        _UnknownThing(),
        ResultMessage(is_error=False, result="ok", session_id="pid"),
    ]
    provider, _ = _make_provider(responses=responses)
    record = await provider.start_session(_issue(), tmp_path / "ws", _claude_config(tmp_path))
    events = [ev async for ev in provider.send_input(record, "x")]
    assert any(e.event == "malformed" for e in events)


# -- interrupt / cancel / close ---------------------------------------------


async def test_interrupt_returns_turn_cancelled_envelope(tmp_path: Path) -> None:
    provider, clients = _make_provider()
    record = await provider.start_session(_issue(), tmp_path / "ws", _claude_config(tmp_path))
    event = await provider.interrupt(record)
    assert event.event == "turn_cancelled"
    assert event.payload["subtype"] == "interrupt"
    methods = [m for m, _ in clients[0].calls]
    assert "interrupt" in methods


async def test_cancel_disconnects_and_marks_terminal(tmp_path: Path) -> None:
    provider, clients = _make_provider()
    record = await provider.start_session(_issue(), tmp_path / "ws", _claude_config(tmp_path))
    event = await provider.cancel(record)
    assert event.event == "session_closed"
    assert event.payload["reason"] == "cancel"
    assert clients[0].disconnected is True
    # Subsequent send_input fails because session is closed.
    with pytest.raises(ProviderError):
        async for _ in provider.send_input(record, "x"):
            pass


async def test_close_is_idempotent_and_safe_after_cancel(tmp_path: Path) -> None:
    provider, _ = _make_provider()
    record = await provider.start_session(_issue(), tmp_path / "ws", _claude_config(tmp_path))
    await provider.close(record)
    await provider.close(record)  # second call is a no-op
    # Closing an unknown session is also safe.
    fake_record = SessionRecord(
        session_id="unknown",
        provider="claude_code",
        issue_identifier="x",
        issue_number=0,
        workspace_path=tmp_path / "ws",
        artifact_dir=tmp_path / "artifacts" / "x" / "1",
        started_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
    )
    await provider.close(fake_record)


# -- Session record persistence shape ----------------------------------------


async def test_session_record_matches_spec_5_4_shape(tmp_path: Path) -> None:
    """SPEC §5.4 lists session_id, provider, provider_session_id (optional),
    issue_identifier, issue_number, workspace_path, artifact_dir,
    transcript_path (optional), attempt, turn_count, started_at,
    last_event_at, terminal_state (optional), previous_provider_session_ids."""
    provider, _ = _make_provider()
    record = await provider.start_session(_issue(), tmp_path / "ws", _claude_config(tmp_path))
    for field_name in (
        "session_id",
        "provider",
        "provider_session_id",
        "issue_identifier",
        "issue_number",
        "workspace_path",
        "artifact_dir",
        "transcript_path",
        "attempt",
        "turn_count",
        "started_at",
        "last_event_at",
        "terminal_state",
        "previous_provider_session_ids",
    ):
        assert hasattr(record, field_name), f"SessionRecord missing {field_name}"
    assert record.provider == "claude_code"
    assert record.attempt == 1
    assert record.turn_count == 0


async def test_send_input_on_unknown_session_raises(tmp_path: Path) -> None:
    provider = ClaudeCodeProvider(client_factory=lambda _o: _FakeClient(_o))
    fake = SessionRecord(
        session_id="never-started",
        provider="claude_code",
        issue_identifier="x",
        issue_number=0,
        workspace_path=tmp_path / "ws",
        artifact_dir=tmp_path / "artifacts" / "x" / "1",
        started_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
    )
    with pytest.raises(ProviderError):
        async for _ in provider.send_input(fake, "x"):
            pass


# -- Session record persistence (#26 leader F1) ------------------------------


async def test_session_record_persisted_after_start_session(tmp_path: Path) -> None:
    """SPEC §5.4 + docs §5.1 phase 2: provider writes the initial session
    record at <session_store>/<session_id>.json with provider_session_id
    null at start_session time, BEFORE any send_input."""
    import json as _json

    provider, _ = _make_provider(session_ids=["sym-persist-1"])
    record = await provider.start_session(_issue(), tmp_path / "ws", _claude_config(tmp_path))
    persisted = tmp_path / "sessions" / "sym-persist-1.json"
    assert persisted.exists(), "start_session must persist the initial record"
    snapshot = _json.loads(persisted.read_text())
    assert snapshot["session_id"] == "sym-persist-1"
    assert snapshot["provider_session_id"] is None
    assert snapshot["attempt"] == 1
    assert record.session_store == tmp_path / "sessions"


async def test_session_record_patched_after_first_send_input_captures_pid(
    tmp_path: Path,
) -> None:
    """Phase 3: after the first send_input, the persisted record is
    patched with the captured provider_session_id. Cross-attempt restore
    relies on this — the previous test only proves the initial null
    write."""
    import json as _json

    responses = [
        AssistantMessage(
            content=[TextBlock(text="hi")],
            session_id="claude-pid-XYZ",
            model="m",
        ),
        ResultMessage(is_error=False, result="ok", session_id="claude-pid-XYZ"),
    ]
    provider, _ = _make_provider(responses=responses, session_ids=["sym-persist-2"])
    record = await provider.start_session(_issue(), tmp_path / "ws", _claude_config(tmp_path))
    async for _ in provider.send_input(record, "first"):
        pass

    persisted = tmp_path / "sessions" / "sym-persist-2.json"
    snapshot = _json.loads(persisted.read_text())
    assert snapshot["provider_session_id"] == "claude-pid-XYZ"
    assert snapshot["last_event_at"] is not None


async def test_restore_persists_bumped_attempt_and_prior_pid(tmp_path: Path) -> None:
    """Phase 4: restore() rewrites the persisted record with the bumped
    attempt and the prior pid appended to previous_provider_session_ids,
    so a crash before the first send_input doesn't lose the audit trail.
    """
    import json as _json

    record = SessionRecord(
        session_id="sym-restore-persist",
        provider="claude_code",
        issue_identifier="acme/proj#1",
        issue_number=1,
        workspace_path=tmp_path / "ws",
        artifact_dir=tmp_path / "artifacts" / "x" / "1",
        started_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        attempt=1,
        provider_session_id="claude-pid-prior",
        session_store=tmp_path / "sessions",
    )
    provider, _ = _make_provider(responses=[])
    restored = await provider.restore(record)

    persisted = tmp_path / "sessions" / "sym-restore-persist.json"
    assert persisted.exists()
    snapshot = _json.loads(persisted.read_text())
    assert snapshot["attempt"] == 2
    assert snapshot["previous_provider_session_ids"] == ["claude-pid-prior"]
    # Round-trip a second restore — pid should not duplicate.
    restored.provider_session_id = "claude-pid-prior"
    await provider.restore(restored)
    snapshot2 = _json.loads(persisted.read_text())
    assert snapshot2["previous_provider_session_ids"] == ["claude-pid-prior"]


async def test_persistence_disabled_when_session_store_is_none(tmp_path: Path) -> None:
    """If a SessionRecord arrives at restore() with session_store=None
    (e.g. legacy test code), persistence is a no-op rather than raising."""
    record = SessionRecord(
        session_id="sym-no-store",
        provider="claude_code",
        issue_identifier="acme/proj#1",
        issue_number=1,
        workspace_path=tmp_path / "ws",
        artifact_dir=tmp_path / "artifacts" / "x" / "1",
        started_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        provider_session_id="claude-pid-prior",
        session_store=None,
    )
    provider, _ = _make_provider(responses=[])
    # Should not raise.
    await provider.restore(record)
    # No file written.
    assert list((tmp_path).rglob("*.json")) == []


async def test_persisted_record_can_drive_restore_after_reload(tmp_path: Path) -> None:
    """Demonstrate that the on-disk record contains everything needed to
    restore: load it back from disk and pass to a fresh provider."""
    import json as _json

    # Phase 1: start + first send_input → persisted record carries pid.
    responses = [
        AssistantMessage(
            content=[TextBlock(text="hi")],
            session_id="claude-pid-roundtrip",
            model="m",
        ),
        ResultMessage(is_error=False, result="ok", session_id="claude-pid-roundtrip"),
    ]
    provider1, _ = _make_provider(responses=responses, session_ids=["sym-roundtrip"])
    record1 = await provider1.start_session(_issue(), tmp_path / "ws", _claude_config(tmp_path))
    async for _ in provider1.send_input(record1, "x"):
        pass
    await provider1.close(record1)

    # Phase 2: a fresh provider loads the persisted record and restores.
    persisted_path = tmp_path / "sessions" / "sym-roundtrip.json"
    snapshot = _json.loads(persisted_path.read_text())
    reloaded = SessionRecord(
        session_id=snapshot["session_id"],
        provider=snapshot["provider"],
        issue_identifier=snapshot["issue_identifier"],
        issue_number=snapshot["issue_number"],
        workspace_path=Path(snapshot["workspace_path"]),
        artifact_dir=Path(snapshot["artifact_dir"]),
        started_at=__import__("datetime").datetime.fromisoformat(snapshot["started_at"]),
        attempt=snapshot["attempt"],
        provider_session_id=snapshot["provider_session_id"],
        previous_provider_session_ids=list(snapshot["previous_provider_session_ids"]),
        session_store=Path(snapshot["session_store"]),
    )
    assert reloaded.provider_session_id == "claude-pid-roundtrip"

    provider2, clients2 = _make_provider(responses=[])
    restored = await provider2.restore(reloaded)
    assert clients2[0].options["resume"] == "claude-pid-roundtrip"
    assert restored.attempt == snapshot["attempt"] + 1
