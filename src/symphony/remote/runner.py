"""Pre-orchestrator composition for remote dispatch execution."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from symphony.artifacts import redact_text
from symphony.config import WorkflowConfig
from symphony.remote.artifacts import ArtifactCollectionResult
from symphony.remote.materialize import (
    MaterializedRemoteDispatch,
    materialize_remote_dispatch_plan,
)
from symphony.remote.plan import RemoteDispatchPlan
from symphony.remote.transport import RemoteRunResult
from symphony.remote.upload import PayloadUploadResult


class PayloadUploader(Protocol):
    """Uploads materialized payloads for a remote dispatch plan."""

    def upload(self, plan: RemoteDispatchPlan) -> PayloadUploadResult:
        """Upload the materialized files for ``plan``."""
        ...


class RemoteTransport(Protocol):
    """Runs the remote worker transport."""

    def run(self, config: WorkflowConfig) -> RemoteRunResult:
        """Run remote worker transport."""
        ...


class RemoteTransportFactory(Protocol):
    """Builds a transport bound to staged remote payload paths."""

    def __call__(
        self,
        *,
        remote_snapshot_path: str,
        remote_dispatch_path: str,
    ) -> RemoteTransport:
        """Return a transport for the staged payload paths."""
        ...


class ArtifactCollector(Protocol):
    """Collects artifacts from remote worker execution."""

    def collect(
        self,
        remote_root: str,
        *,
        owner: str,
        repo: str,
        issue_number: int,
        attempt: int,
    ) -> ArtifactCollectionResult:
        """Collect artifacts from remote path to local artifact store."""
        ...


@dataclass(frozen=True, slots=True)
class RemoteDispatchRunResult:
    """Combined result for a pre-orchestrator remote dispatch run."""

    materialized: MaterializedRemoteDispatch | None = None
    upload: PayloadUploadResult | None = None
    transport: RemoteRunResult | None = None
    artifacts: ArtifactCollectionResult | None = None
    errors: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.failed

    @property
    def failed(self) -> bool:
        return bool(self.errors or (self.transport is not None and self.transport.failed))


@dataclass(slots=True)
class RemoteDispatchRunner:
    """Composes materialization, payload upload, SSH transport, and artifact collection."""

    uploader: PayloadUploader
    transport_factory: RemoteTransportFactory
    artifact_collector: ArtifactCollector | None = None

    def run(self, plan: RemoteDispatchPlan, config: WorkflowConfig) -> RemoteDispatchRunResult:
        """Run one remote dispatch plan without orchestrator scheduling."""
        try:
            materialized = materialize_remote_dispatch_plan(plan, config)
        except OSError as exc:
            return RemoteDispatchRunResult(errors=(f"materialization failed: {exc}",))

        upload = self.uploader.upload(plan)
        if not upload.ok:
            return RemoteDispatchRunResult(
                materialized=materialized,
                upload=upload,
                errors=tuple(f"upload failed: {error}" for error in upload.errors),
            )

        transport = self.transport_factory(
            remote_snapshot_path=plan.remote_snapshot_path,
            remote_dispatch_path=plan.remote_dispatch_path,
        )
        transport_result = transport.run(config)

        # Collect artifacts after transport completes (even if transport failed)
        artifacts_result = None
        if self.artifact_collector:
            try:
                artifacts_result = self.artifact_collector.collect(
                    plan.remote_artifact_path,
                    owner=plan.dispatch_request.owner,
                    repo=plan.dispatch_request.repo,
                    issue_number=plan.dispatch_request.issue_number,
                    attempt=plan.dispatch_request.attempt,
                )
            except Exception as exc:
                error = redact_text(
                    f"artifact collection failed: {exc}",
                    redact_keys=("token", "authorization", "api_key", "password", "secret"),
                    extra_secrets=(config.tracker.token,),
                )
                artifacts_result = ArtifactCollectionResult(
                    local_root=_local_artifact_root(plan, config),
                    errors=(error,),
                )
        artifact_errors = artifacts_result.errors if artifacts_result is not None else ()

        return RemoteDispatchRunResult(
            materialized=materialized,
            upload=upload,
            transport=transport_result,
            artifacts=artifacts_result,
            errors=transport_result.errors + artifact_errors,
        )


def _local_artifact_root(plan: RemoteDispatchPlan, config: WorkflowConfig):
    request = plan.dispatch_request
    return (
        config.claude.artifact_store
        / f"{request.owner}_{request.repo}_{request.issue_number}"
        / str(request.attempt)
    )
