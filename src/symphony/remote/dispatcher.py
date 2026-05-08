"""Orchestrator-facing remote dispatch adapter.

Provides a narrow protocol for the orchestrator to dispatch issues to remote
workers without depending on concrete SSH/SCP implementations. The production
adapter composes the pure plan builder with the remote runner; tests can still
inject a small fake dispatcher.
"""

from __future__ import annotations

from typing import Protocol

from symphony.config import WorkflowConfig
from symphony.models import Issue
from symphony.remote.plan import build_remote_dispatch_plan
from symphony.remote.runner import RemoteDispatchRunner, RemoteDispatchRunResult
from symphony.remote.scp import RealScpRunner, SSHArtifactCollector
from symphony.remote.ssh import RealSubprocessRunner, SSHRemoteTransport
from symphony.remote.upload import RealPayloadUploadRunner, SCPPayloadUploader


class RemoteIssueDispatcher(Protocol):
    """Dispatches one issue to a remote worker."""

    def dispatch(
        self,
        issue: Issue,
        *,
        attempt: int,
        config: WorkflowConfig,
    ) -> RemoteDispatchRunResult:
        """Dispatch issue to remote worker and return outcome."""
        ...


class RunnerRemoteIssueDispatcher:
    """RemoteIssueDispatcher backed by RemoteDispatchRunner."""

    def __init__(self, runner: RemoteDispatchRunner) -> None:
        self._runner = runner

    def dispatch(
        self,
        issue: Issue,
        *,
        attempt: int,
        config: WorkflowConfig,
    ) -> RemoteDispatchRunResult:
        plan = build_remote_dispatch_plan(issue, attempt=attempt, config=config)
        return self._runner.run(plan, config)


def build_ssh_remote_issue_dispatcher(
    config: WorkflowConfig,
) -> RemoteIssueDispatcher | None:
    """Build the production SSH/SCP remote dispatcher.

    Returns ``None`` when remote execution is disabled so callers can pass the
    result directly into ``Orchestrator`` for both local and remote runs.
    """
    if not config.remote.enabled:
        return None
    if not config.remote.host:
        raise ValueError("remote.host is required for SSH remote dispatch")

    extra_secrets = tuple(
        secret for secret in (config.tracker.token, config.remote.git_token) if secret
    )
    uploader = SCPPayloadUploader(
        host=config.remote.host,
        runner=RealPayloadUploadRunner(),
        extra_secrets=extra_secrets,
    )
    artifact_collector = SSHArtifactCollector(
        artifact_store=config.claude.artifact_store,
        redact_keys=tuple(config.logging.redact_keys) + ("git_token",),
        runner=RealScpRunner(),
        host=config.remote.host,
    )

    def transport_factory(
        *,
        remote_snapshot_path: str,
        remote_dispatch_path: str,
    ) -> SSHRemoteTransport:
        return SSHRemoteTransport(
            runner=RealSubprocessRunner(),
            remote_snapshot_path=remote_snapshot_path,
            remote_dispatch_path=remote_dispatch_path,
        )

    runner = RemoteDispatchRunner(
        uploader=uploader,
        transport_factory=transport_factory,
        artifact_collector=artifact_collector,
    )
    return RunnerRemoteIssueDispatcher(runner)
