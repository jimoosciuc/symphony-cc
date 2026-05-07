"""Opt-in live integration tests for the Claude Code provider.

Skipped by default. Enabled when ALL of:

- ``SYMPHONY_RUN_CLAUDE_INTEGRATION=1``
- The ``claude`` CLI is on ``PATH`` and authenticated.
- ``claude-agent-sdk`` is importable.

The default test workspace is a tmpdir created per-test; each test
sends a short prompt that should complete in seconds. Skipped tests
report as skipped, not silently passed (per SPEC §21).
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from symphony.config import ClaudeConfig
from symphony.models import Issue
from symphony.provider import ClaudeCodeProvider

_GATE_ENV = "SYMPHONY_RUN_CLAUDE_INTEGRATION"


def _gate() -> None:
    if os.environ.get(_GATE_ENV) != "1":
        pytest.skip(f"{_GATE_ENV} not set; live Claude tests skipped")
    if shutil.which("claude") is None:
        pytest.skip("`claude` CLI not on PATH; live Claude tests skipped")
    try:
        import claude_agent_sdk  # noqa: F401
    except ImportError:
        pytest.skip("claude-agent-sdk not installed; live Claude tests skipped")


def _live_issue() -> Issue:
    return Issue(
        id="I_live",
        number=999,
        identifier="symphony/live#999",
        owner="symphony",
        repo="live",
        title="Live integration smoke",
        body="Reply with the literal text OK and nothing else.",
        state="open",
        url="https://github.com/symphony/live/issues/999",
    )


def _live_config(tmp_path: Path) -> ClaudeConfig:
    return ClaudeConfig(
        model=os.environ.get("SYMPHONY_CLAUDE_TEST_MODEL", "claude-sonnet-4-5"),
        permission_mode="acceptEdits",
        session_store=tmp_path / "sessions",
        transcript_store=tmp_path / "transcripts",
        artifact_store=tmp_path / "artifacts",
        # Tighten budgets so a hung run fails fast in CI.
        turn_timeout_ms=120_000,
        stall_timeout_ms=60_000,
    )


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    return ws


async def test_live_smoke_one_turn_completes(tmp_path: Path, workspace: Path) -> None:
    """End-to-end: start_session, send_input one prompt, assert the
    stream terminates with turn_completed and the session record carries
    a provider_session_id."""
    _gate()
    provider = ClaudeCodeProvider()
    record = await provider.start_session(_live_issue(), workspace, _live_config(tmp_path))
    try:
        terminal_seen = None
        async for ev in provider.send_input(
            record, "Reply with the literal text OK and nothing else."
        ):
            if ev.event in {"turn_completed", "turn_failed", "turn_cancelled"}:
                terminal_seen = ev.event
                break
        assert terminal_seen == "turn_completed"
        assert record.provider_session_id is not None
    finally:
        await provider.close(record)


async def test_live_continuation_reuses_provider_session_id(
    tmp_path: Path, workspace: Path
) -> None:
    """Second send_input on the same session must reuse the
    provider_session_id captured from the first turn."""
    _gate()
    provider = ClaudeCodeProvider()
    record = await provider.start_session(_live_issue(), workspace, _live_config(tmp_path))
    try:
        async for _ in provider.send_input(record, "Reply with OK."):
            pass
        first_pid = record.provider_session_id
        assert first_pid is not None
        async for _ in provider.send_input(record, "Reply with DONE."):
            pass
        assert record.provider_session_id == first_pid
    finally:
        await provider.close(record)
