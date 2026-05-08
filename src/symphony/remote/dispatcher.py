"""Orchestrator-facing remote issue dispatch boundary."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from symphony.config import WorkflowConfig
from symphony.models import Issue
from symphony.remote.plan import build_remote_dispatch_plan
from symphony.remote.runner import RemoteDispatchRunner, RemoteDispatchRunResult
from symphony.remote.scp import SSHArtifactCollector
from symphony.remote.ssh import RealSubprocessRunner, SSHRemoteTransport
from symphony.remote.upload import RealPayloadUploadRunner, SCPPayloadUploader


class RemoteIssueDispatcher(Protocol):
    """Dispatches one claimed issue through the remote execution pipeline."""

    def dispatch(
        self,
        issue: Issue,
        *,
        attempt: int,
        config: WorkflowConfig,
    ) -> RemoteDispatchRunResult:
        """Run one remote dispatch attempt for ``issue``."""
        ...


@dataclass(frozen=True, slots=True)
class RunnerRemoteIssueDispatcher:
    """Adapter from orchestrator issue context to ``RemoteDispatchRunner``."""

    runner: RemoteDispatchRunner

    def dispatch(
        self,
        issue: Issue,
        *,
        attempt: int,
        config: WorkflowConfig,
    ) -> RemoteDispatchRunResult:
        plan = build_remote_dispatch_plan(issue, attempt=attempt, config=config)
        return self.runner.run(plan, config)


def build_ssh_remote_issue_dispatcher(config: WorkflowConfig) -> RemoteIssueDispatcher | None:
    """Build the production SSH/SCP dispatcher when remote execution is enabled."""
    if not config.remote.enabled:
        return None
    if not config.remote.host:
        raise ValueError("remote.host is required for remote dispatch")

    upload_runner = RealPayloadUploadRunner()
    ssh_runner = RealSubprocessRunner()
    uploader = SCPPayloadUploader(
        host=config.remote.host,
        runner=upload_runner,
        extra_secrets=(config.tracker.token,),
    )
    artifact_collector = SSHArtifactCollector(
        artifact_store=config.claude.artifact_store,
        redact_keys=config.logging.redact_keys,
        runner=upload_runner,
        host=config.remote.host,
    )

    def transport_factory(
        *,
        remote_snapshot_path: str,
        remote_dispatch_path: str,
    ) -> SSHRemoteTransport:
        return SSHRemoteTransport(
            runner=ssh_runner,
            remote_snapshot_path=remote_snapshot_path,
            remote_dispatch_path=remote_dispatch_path,
        )

    return RunnerRemoteIssueDispatcher(
        RemoteDispatchRunner(
            uploader=uploader,
            transport_factory=transport_factory,
            artifact_collector=artifact_collector,
        )
    )
