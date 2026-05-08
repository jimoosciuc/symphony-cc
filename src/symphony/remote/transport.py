"""Fakeable remote worker transport primitives."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from symphony.config import WorkflowConfig
from symphony.remote.protocol import ProtocolError, WorkerEvent, parse_worker_event


class RemoteTransportProtocol(Protocol):
    """Transport boundary for remote worker event streaming."""

    def run(self, config: WorkflowConfig) -> RemoteRunResult:
        """Run a remote worker and return parsed protocol events."""


@dataclass(frozen=True, slots=True)
class RemoteRunResult:
    events: tuple[WorkerEvent, ...] = ()
    errors: tuple[str, ...] = ()
    failed: bool = False
    stalled: bool = False

    @property
    def ok(self) -> bool:
        return not self.failed and not self.stalled and not self.errors


@dataclass(slots=True)
class FakeRemoteWorker:
    """Local test worker that validates config and emits fixed JSONL lines."""

    lines: tuple[str, ...]

    def run(self, config: WorkflowConfig) -> tuple[str, ...]:
        validate_remote_worker_config(config)
        return self.lines


@dataclass(slots=True)
class FakeRemoteTransport:
    """Deterministic transport used by default CI tests."""

    worker: FakeRemoteWorker
    heartbeat_deadline_missed: bool = False

    def run(self, config: WorkflowConfig) -> RemoteRunResult:
        events: list[WorkerEvent] = []
        errors: list[str] = []
        failed = False
        for line in self.worker.run(config):
            try:
                event = parse_worker_event(line)
            except ProtocolError as exc:
                errors.append(str(exc))
                continue
            events.append(event)
            if event.event == "worker_failed":
                failed = True
        return RemoteRunResult(
            events=tuple(events),
            errors=tuple(errors),
            failed=failed,
            stalled=self.heartbeat_deadline_missed,
        )


def validate_remote_worker_config(config: WorkflowConfig) -> None:
    """Defensive remote-side config snapshot validation."""

    if not config.remote.enabled:
        raise ValueError("remote.enabled must be true for remote worker execution")
    missing = [
        key
        for key in ("host", "workspace_root", "artifact_root", "session_store")
        if not getattr(config.remote, key)
    ]
    if missing:
        raise ValueError(f"remote config missing required fields: {', '.join(missing)}")
