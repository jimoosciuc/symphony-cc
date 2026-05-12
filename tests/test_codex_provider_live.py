"""Opt-in live integration tests for the Codex provider.

Skipped by default. Enabled when all of:

- ``SYMPHONY_RUN_CODEX_INTEGRATION=1``
- the ``codex`` CLI is on ``PATH`` and authenticated
- a usable Codex model is available

These tests intentionally use a temporary workspace and do not touch GitHub.
They validate the real subprocess path that fake-runner tests cannot cover:
reply events, workspace writes, and ``codex exec resume`` continuity.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from symphony.config import ClaudeConfig
from symphony.models import Issue
from symphony.provider import CodexProvider

_GATE_ENV = "SYMPHONY_RUN_CODEX_INTEGRATION"

pytestmark = pytest.mark.codex_live


def _gate() -> None:
    if os.environ.get(_GATE_ENV) != "1":
        pytest.skip(f"{_GATE_ENV} not set; live Codex tests skipped")
    if shutil.which(os.environ.get("SYMPHONY_CODEX_BIN", "codex")) is None:
        pytest.skip("`codex` CLI not on PATH; live Codex tests skipped")


def _live_issue() -> Issue:
    return Issue(
        id="I_codex_live",
        number=1001,
        identifier="symphony/live#1001",
        owner="symphony",
        repo="live",
        title="Live Codex integration smoke",
        body="Live Codex smoke test.",
        state="open",
        url="https://github.com/symphony/live/issues/1001",
    )


def _live_config(tmp_path: Path) -> ClaudeConfig:
    return ClaudeConfig(
        model=os.environ.get("SYMPHONY_CODEX_TEST_MODEL", "gpt-5.3-codex"),
        permission_mode=os.environ.get("SYMPHONY_CODEX_TEST_PERMISSION_MODE", "acceptEdits"),
        session_store=tmp_path / "sessions",
        transcript_store=tmp_path / "transcripts",
        artifact_store=tmp_path / "artifacts",
        turn_timeout_ms=120_000,
        stall_timeout_ms=60_000,
    )


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    return ws


async def _drain(provider: CodexProvider, record, message: str) -> list[str]:
    events: list[str] = []
    async for event in provider.send_input(record, message):
        events.append(event.event)
    return events


async def test_live_codex_reply_write_and_resume(
    tmp_path: Path,
    workspace: Path,
) -> None:
    _gate()
    provider = CodexProvider()
    record = await provider.start_session(_live_issue(), workspace, _live_config(tmp_path))
    try:
        first_events = await _drain(
            provider,
            record,
            "Reply exactly: CODEX_LIVE_REPLY_OK",
        )
        first_provider_session_id = record.provider_session_id
        assert first_events[-1] == "turn_completed"
        assert first_provider_session_id is not None
        assert (
            record.artifact_dir / "codex-last-message.txt"
        ).read_text(encoding="utf-8").strip() == "CODEX_LIVE_REPLY_OK"

        second_events = await _drain(
            provider,
            record,
            (
                "Create a file named codex-live-result.txt containing exactly "
                "CODEX_LIVE_WRITE_OK, then reply Done."
            ),
        )
        assert second_events[-1] == "turn_completed"
        assert record.provider_session_id == first_provider_session_id
        assert (
            workspace / "codex-live-result.txt"
        ).read_text(encoding="utf-8").strip() == "CODEX_LIVE_WRITE_OK"
    finally:
        await provider.close(record)
