from __future__ import annotations

from pathlib import Path

import pytest

from symphony.config import ClaudeConfig
from symphony.events import AgentEvent
from symphony.models import Issue
from symphony.provider import CodexProvider
from symphony.provider.base import AgentProviderProtocol, ProviderError
from symphony.provider.codex import CodexRunResult, _build_codex_command


def _issue(number: int = 1) -> Issue:
    return Issue(
        id=f"I_{number}",
        number=number,
        identifier=f"acme/proj#{number}",
        owner="acme",
        repo="proj",
        title="Codex task",
        body="body",
        state="open",
        url=f"https://github.com/acme/proj/issues/{number}",
    )


def _config(tmp_path: Path, *, model: str = "gpt-5.3-codex") -> ClaudeConfig:
    return ClaudeConfig(
        model=model,
        permission_mode="acceptEdits",
        session_store=tmp_path / "sessions",
        transcript_store=tmp_path / "transcripts",
        artifact_store=tmp_path / "artifacts",
    )


async def _drain(stream) -> list[AgentEvent]:
    return [event async for event in stream]


def test_codex_provider_satisfies_protocol() -> None:
    provider = CodexProvider()
    assert isinstance(provider, AgentProviderProtocol)
    assert provider.name == "codex"


@pytest.mark.asyncio
async def test_codex_provider_normalizes_reply_and_usage(tmp_path: Path) -> None:
    async def runner(session, message, config, provider_session_id):
        assert message == "go"
        assert config.model == "gpt-5.3-codex"
        assert provider_session_id is None
        return CodexRunResult(
            returncode=0,
            events=[
                {"type": "thread.started", "thread_id": "thread-1"},
                {"type": "turn.started"},
                {
                    "type": "item.completed",
                    "item": {"id": "item_0", "type": "agent_message", "text": "done"},
                },
                {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 2}},
            ],
            stderr="model refresh warning",
            last_message="done",
        )

    provider = CodexProvider(runner=runner, session_id_factory=lambda: "sym-test")
    record = await provider.start_session(_issue(), tmp_path / "ws", _config(tmp_path))

    events = await _drain(provider.send_input(record, "go"))

    assert [event.event for event in events] == [
        "session_started",
        "heartbeat",
        "message_delta",
        "message_completed",
        "usage",
        "turn_completed",
    ]
    assert record.provider_session_id == "thread-1"
    assert record.turn_count == 1
    assert events[-1].payload["stderr"] == "model refresh warning"


@pytest.mark.asyncio
async def test_codex_provider_resumes_with_existing_thread(tmp_path: Path) -> None:
    calls: list[str | None] = []

    async def runner(session, message, config, provider_session_id):
        calls.append(provider_session_id)
        thread_id = provider_session_id or "thread-1"
        return CodexRunResult(
            returncode=0,
            events=[
                {"type": "thread.started", "thread_id": thread_id},
                {
                    "type": "item.completed",
                    "item": {"id": "item_0", "type": "agent_message", "text": message},
                },
                {"type": "turn.completed", "usage": {}},
            ],
        )

    provider = CodexProvider(runner=runner, session_id_factory=lambda: "sym-test")
    record = await provider.start_session(_issue(), tmp_path / "ws", _config(tmp_path))

    first = await _drain(provider.send_input(record, "first"))
    second = await _drain(provider.send_input(record, "second"))

    assert calls == [None, "thread-1"]
    assert first[0].event == "session_started"
    assert all(event.event != "session_started" for event in second)


@pytest.mark.asyncio
async def test_codex_provider_normalizes_command_execution(tmp_path: Path) -> None:
    async def runner(session, message, config, provider_session_id):
        return CodexRunResult(
            returncode=0,
            events=[
                {"type": "thread.started", "thread_id": "thread-1"},
                {
                    "type": "item.started",
                    "item": {
                        "id": "cmd-1",
                        "type": "command_execution",
                        "command": "/bin/zsh -lc 'true'",
                        "status": "in_progress",
                        "exit_code": None,
                    },
                },
                {
                    "type": "item.completed",
                    "item": {
                        "id": "cmd-1",
                        "type": "command_execution",
                        "command": "/bin/zsh -lc 'true'",
                        "status": "completed",
                        "exit_code": 0,
                        "aggregated_output": "",
                    },
                },
                {"type": "turn.completed", "usage": {}},
            ],
        )

    provider = CodexProvider(runner=runner)
    record = await provider.start_session(_issue(), tmp_path / "ws", _config(tmp_path))

    events = await _drain(provider.send_input(record, "go"))

    assert [event.event for event in events] == [
        "session_started",
        "tool_started",
        "tool_completed",
        "usage",
        "turn_completed",
    ]
    assert events[1].payload["command"] == "/bin/zsh -lc 'true'"
    assert events[2].payload["exit_code"] == 0


@pytest.mark.asyncio
async def test_codex_provider_nonzero_exit_yields_turn_failed(tmp_path: Path) -> None:
    async def runner(session, message, config, provider_session_id):
        return CodexRunResult(
            returncode=2,
            events=[{"type": "thread.started", "thread_id": "thread-1"}],
            stderr="boom",
        )

    provider = CodexProvider(runner=runner)
    record = await provider.start_session(_issue(), tmp_path / "ws", _config(tmp_path))

    events = await _drain(provider.send_input(record, "go"))

    assert events[0].event == "session_started"
    assert events[-1].event == "turn_failed"
    assert events[-1].payload["returncode"] == 2
    assert events[-1].payload["stderr"] == "boom"


@pytest.mark.asyncio
async def test_codex_provider_malformed_jsonl_is_reported(tmp_path: Path) -> None:
    async def runner(session, message, config, provider_session_id):
        return CodexRunResult(
            returncode=0,
            events=[
                {"type": "thread.started", "thread_id": "thread-1"},
                {"type": "turn.completed", "usage": {}},
            ],
            malformed_lines=["not-json"],
        )

    provider = CodexProvider(runner=runner)
    record = await provider.start_session(_issue(), tmp_path / "ws", _config(tmp_path))

    events = await _drain(provider.send_input(record, "go"))

    assert events[0].event == "malformed"
    assert events[0].payload["line"] == "not-json"
    assert events[-1].event == "turn_completed"


@pytest.mark.asyncio
async def test_codex_provider_send_input_requires_open_session(tmp_path: Path) -> None:
    provider = CodexProvider(runner=lambda *_args: None)  # type: ignore[arg-type]
    record = await provider.start_session(_issue(), tmp_path / "ws", _config(tmp_path))
    await provider.close(record)

    with pytest.raises(ProviderError):
        await _drain(provider.send_input(record, "go"))


def test_codex_command_uses_top_level_cd_and_resume(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    cmd = _build_codex_command(
        codex_bin="codex",
        config=cfg,
        workspace=tmp_path / "ws",
        last_message_path=tmp_path / "last.txt",
        provider_session_id="thread-1",
    )

    assert cmd[:2] == ["codex", "-a"]
    assert "resume" in cmd
    assert "thread-1" in cmd
    assert "-C" in cmd
    assert "--cd" not in cmd
    assert cmd[-1] == "-"
