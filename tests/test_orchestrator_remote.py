"""Remote-dispatch orchestrator wiring tests."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from symphony.config import (
    AgentConfig,
    ClaudeConfig,
    GitHubConfig,
    LoggingConfig,
    PollingConfig,
    RetryConfig,
    SecurityConfig,
    TrackerConfig,
    WorkflowConfig,
    WorkspaceConfig,
)
from symphony.github.tracker import ClaimResult, FakeGitHubTracker
from symphony.models import Issue
from symphony.orchestrator import Orchestrator
from symphony.provider.fake import FakeProvider
from symphony.remote.config import RemoteConfig
from symphony.remote.runner import RemoteDispatchRunResult
from symphony.remote.transport import RemoteRunResult
from symphony.retry import RetryState
from symphony.workspace import WorkspaceManager


def _issue(number: int = 1) -> Issue:
    return Issue(
        id=f"I_{number}",
        number=number,
        identifier=f"acme/proj#{number}",
        owner="acme",
        repo="proj",
        title=f"Issue {number}",
        body=f"Body {number}",
        state="open",
        url=f"https://github.com/acme/proj/issues/{number}",
        labels=("symphony-ready",),
    )


def _config(tmp_path: Path, *, remote_enabled: bool) -> WorkflowConfig:
    return WorkflowConfig(
        tracker=TrackerConfig(
            kind="github",
            owner="acme",
            repo="proj",
            token="literal-token",
            include_labels=("symphony-ready",),
            exclude_labels=("symphony-blocked",),
        ),
        agent=AgentConfig(max_concurrency=1, max_turns=1),
        workspace=WorkspaceConfig(root=tmp_path / "ws"),
        claude=ClaudeConfig(
            model="fake-model",
            permission_mode="acceptEdits",
            session_store=tmp_path / "sessions",
            transcript_store=tmp_path / "transcripts",
            artifact_store=tmp_path / "artifacts",
        ),
        github=GitHubConfig(),
        security=SecurityConfig(profile="trusted_unattended"),
        polling=PollingConfig(),
        retry=RetryConfig(initial_backoff_ms=1000, max_backoff_ms=8000, multiplier=2.0),
        logging=LoggingConfig(),
        remote=RemoteConfig(
            enabled=remote_enabled,
            host="user@remote-host" if remote_enabled else None,
            workspace_root="/remote/workspaces" if remote_enabled else None,
            artifact_root="/remote/artifacts" if remote_enabled else None,
            session_store="/remote/sessions" if remote_enabled else None,
        ),
        workflow_path=tmp_path / "WORKFLOW.md",
    )


class FakeRemoteDispatcher:
    def __init__(self, result: RemoteDispatchRunResult | Exception) -> None:
        self.result = result
        self.calls: list[dict[str, object]] = []

    def dispatch(
        self,
        issue: Issue,
        *,
        attempt: int,
        config: WorkflowConfig,
    ) -> RemoteDispatchRunResult:
        self.calls.append(
            {
                "issue": issue.identifier,
                "attempt": attempt,
                "remote_enabled": config.remote.enabled,
            }
        )
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _orchestrator(
    tmp_path: Path,
    *,
    issue: Issue,
    remote_enabled: bool,
    remote_dispatcher: FakeRemoteDispatcher | None,
):
    config = _config(tmp_path, remote_enabled=remote_enabled)
    tracker = FakeGitHubTracker(issues=[issue])
    provider = FakeProvider()
    orchestrator = Orchestrator(
        config,
        tracker=tracker,
        provider=provider,
        workspace_manager=WorkspaceManager(config.workspace),
        remote_dispatcher=remote_dispatcher,
    )
    return orchestrator, tracker, provider


async def test_remote_disabled_uses_local_provider_path(tmp_path: Path) -> None:
    issue = _issue()
    remote = FakeRemoteDispatcher(RemoteDispatchRunResult())
    orch, _tracker, provider = _orchestrator(
        tmp_path,
        issue=issue,
        remote_enabled=False,
        remote_dispatcher=remote,
    )

    result = await orch.run_once()

    assert result.dispatched == [issue.identifier]
    assert result.finished == [issue.identifier]
    assert remote.calls == []
    assert [method for method, _ in provider.calls] == [
        "start_session",
        "send_input",
        "close",
    ]


async def test_remote_enabled_uses_remote_dispatcher_not_provider(tmp_path: Path) -> None:
    issue = _issue()
    remote = FakeRemoteDispatcher(RemoteDispatchRunResult(transport=RemoteRunResult()))
    orch, tracker, provider = _orchestrator(
        tmp_path,
        issue=issue,
        remote_enabled=True,
        remote_dispatcher=remote,
    )

    result = await orch.run_once()

    assert result.dispatched == [issue.identifier]
    assert result.finished == [issue.identifier]
    assert result.retries_scheduled == []
    assert remote.calls == [
        {"issue": issue.identifier, "attempt": 1, "remote_enabled": True}
    ]
    assert provider.calls == []
    history = [entry for _, entry in tracker.states[issue.identifier].claim_history]
    assert history[0].startswith("claim:")
    assert history[-1] == "release:remote_completed"


async def test_remote_claim_conflict_prevents_remote_dispatch(tmp_path: Path) -> None:
    issue = _issue()
    remote = FakeRemoteDispatcher(RemoteDispatchRunResult(transport=RemoteRunResult()))
    orch, _tracker, provider = _orchestrator(
        tmp_path,
        issue=issue,
        remote_enabled=True,
        remote_dispatcher=remote,
    )
    orch.tracker.claim_issue = lambda issue, run_metadata: ClaimResult(
        ok=False,
        conflict=True,
    )

    result = await orch.run_once()

    assert result.dispatched == []
    assert result.finished == []
    assert result.skipped_claim_conflict == [issue.identifier]
    assert remote.calls == []
    assert provider.calls == []


async def test_remote_failure_schedules_retry_not_completed_success(tmp_path: Path) -> None:
    issue = _issue()
    remote = FakeRemoteDispatcher(
        RemoteDispatchRunResult(
            transport=RemoteRunResult(errors=("SSH failed",), failed=True),
            errors=("SSH failed",),
        )
    )
    orch, tracker, provider = _orchestrator(
        tmp_path,
        issue=issue,
        remote_enabled=True,
        remote_dispatcher=remote,
    )

    result = await orch.run_once()

    assert result.dispatched == [issue.identifier]
    assert result.finished == [issue.identifier]
    assert result.retries_scheduled == [issue.identifier]
    assert provider.calls == []
    assert orch.retry_states[issue.identifier].next_attempt_at is not None
    history = [entry for _, entry in tracker.states[issue.identifier].claim_history]
    assert history[-1] == "release:remote_failed"
    terminal = next((tmp_path / "artifacts").rglob("terminal.json")).read_text()
    assert '"terminal_state": "failed"' in terminal
    assert '"reason": "remote_failed"' in terminal


async def test_remote_errors_are_redacted_in_state_and_artifacts(tmp_path: Path) -> None:
    issue = _issue()
    remote = FakeRemoteDispatcher(
        RemoteDispatchRunResult(
            transport=RemoteRunResult(
                errors=("upload failed with literal-token",),
                failed=True,
            ),
            errors=("upload failed with literal-token",),
        )
    )
    orch, tracker, _provider = _orchestrator(
        tmp_path,
        issue=issue,
        remote_enabled=True,
        remote_dispatcher=remote,
    )

    await orch.run_once()

    assert "literal-token" not in orch.retry_states[issue.identifier].last_error
    history = " ".join(
        entry for _, entry in tracker.states[issue.identifier].claim_history
    )
    assert "literal-token" not in history
    artifact_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (tmp_path / "artifacts").rglob("*.json")
    )
    assert "literal-token" not in artifact_text
    assert "<redacted>" in artifact_text


async def test_remote_retry_exhaustion_blocks_issue(tmp_path: Path) -> None:
    issue = _issue()
    config = replace(_config(tmp_path, remote_enabled=True), retry=RetryConfig(max_attempts=1))
    tracker = FakeGitHubTracker(issues=[issue])
    provider = FakeProvider()
    remote = FakeRemoteDispatcher(
        RemoteDispatchRunResult(errors=("remote failed",), transport=RemoteRunResult(failed=True))
    )
    orch = Orchestrator(
        config,
        tracker=tracker,
        provider=provider,
        workspace_manager=WorkspaceManager(config.workspace),
        remote_dispatcher=remote,
    )
    orch.retry_states[issue.identifier] = RetryState(
        issue_identifier=issue.identifier,
        attempts=1,
    )

    result = await orch.run_once()

    assert result.retries_scheduled == []
    assert tracker.states[issue.identifier].blocked
    history = [entry for _, entry in tracker.states[issue.identifier].claim_history]
    assert history[-1] == "blocked:remote failed"
