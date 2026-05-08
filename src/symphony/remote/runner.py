"""Pre-orchestrator composition for remote dispatch execution."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from symphony.config import WorkflowConfig
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


@dataclass(frozen=True, slots=True)
class RemoteDispatchRunResult:
    """Combined result for a pre-orchestrator remote dispatch run."""

    materialized: MaterializedRemoteDispatch | None = None
    upload: PayloadUploadResult | None = None
    transport: RemoteRunResult | None = None
    errors: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.failed

    @property
    def failed(self) -> bool:
        return bool(self.errors or (self.transport is not None and self.transport.failed))


@dataclass(slots=True)
class RemoteDispatchRunner:
    """Composes materialization, payload upload, and SSH transport execution."""

    uploader: PayloadUploader
    transport_factory: RemoteTransportFactory

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

        return RemoteDispatchRunResult(
            materialized=materialized,
            upload=upload,
            transport=transport_result,
            errors=transport_result.errors,
        )
