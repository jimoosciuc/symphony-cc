"""Workflow reload support (#70).

The reloader owns last-known-good workflow snapshots and the metadata
polling trigger. It deliberately sits beside the workflow loader rather
than inside the orchestrator so reload validation can be tested without
standing up GitHub/provider fakes.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from symphony.artifact_retention import REPORT_DIR_NAME
from symphony.artifacts import redact
from symphony.config import WorkflowConfig
from symphony.workflow import WorkflowFile, load_workflow

_LOG = logging.getLogger("symphony.workflow_reload")


@dataclass(frozen=True, slots=True)
class WorkflowFileMetadata:
    mtime_ns: int
    size: int
    inode: int | None = None


@dataclass(frozen=True, slots=True)
class WorkflowSnapshot:
    workflow: WorkflowFile | None
    config: WorkflowConfig
    workflow_path: Path | None
    metadata: WorkflowFileMetadata | None
    revision: int
    loaded_at: datetime


@dataclass(frozen=True, slots=True)
class WorkflowReloadResult:
    current_snapshot: WorkflowSnapshot
    changed: bool = False
    reloaded: bool = False
    error: str | None = None
    dispatch_paused: bool = False


class WorkflowReloader:
    """Polls ``WORKFLOW.md`` metadata and publishes last-known-good snapshots."""

    def __init__(
        self,
        snapshot: WorkflowSnapshot,
        *,
        env: dict[str, str] | None = None,
        clock: object | None = None,
    ) -> None:
        self.snapshot = snapshot
        self._env = env
        self._clock = clock or _now_utc

    @classmethod
    def from_workflow(
        cls,
        workflow: WorkflowFile,
        *,
        env: dict[str, str] | None = None,
        clock: object | None = None,
    ) -> WorkflowReloader:
        now = (clock or _now_utc)()
        return cls(
            WorkflowSnapshot(
                workflow=workflow,
                config=workflow.config,
                workflow_path=workflow.path,
                metadata=_stat_metadata(workflow.path),
                revision=1,
                loaded_at=now,
            ),
            env=env,
            clock=clock,
        )

    @classmethod
    def from_config(
        cls,
        config: WorkflowConfig,
        *,
        env: dict[str, str] | None = None,
        clock: object | None = None,
    ) -> WorkflowReloader | None:
        if config.workflow_path is None:
            return None
        path = Path(config.workflow_path)
        if not path.is_file():
            return None
        workflow = load_workflow(path, env=env)
        return cls.from_workflow(workflow, env=env, clock=clock)

    def maybe_reload(self) -> WorkflowReloadResult:
        path = self.snapshot.workflow_path
        if path is None:
            return WorkflowReloadResult(current_snapshot=self.snapshot)
        try:
            metadata = _stat_metadata(path)
        except OSError as exc:
            error = f"workflow metadata read failed: {exc}"
            self._record_reload_event(outcome="rejected_parse", error=error)
            _LOG.warning("workflow reload failed: %s", error)
            return WorkflowReloadResult(
                current_snapshot=self.snapshot,
                changed=True,
                error=error,
                dispatch_paused=True,
            )
        if metadata == self.snapshot.metadata:
            return WorkflowReloadResult(current_snapshot=self.snapshot)

        try:
            workflow = load_workflow(path, env=self._env)
            rejected = _restart_required_changes(self.snapshot.config, workflow.config)
            if rejected:
                fields = ", ".join(rejected)
                error = f"restart-required workflow field changed: {fields}"
                self._record_reload_event(
                    outcome="rejected_validation",
                    changed_fields=rejected,
                    error=error,
                )
                _LOG.warning("workflow reload rejected: %s", error)
                return WorkflowReloadResult(
                    current_snapshot=self.snapshot,
                    changed=True,
                    error=error,
                    dispatch_paused=True,
                )
        except Exception as exc:  # noqa: BLE001 - parser/config errors share one surface
            error = str(exc)
            self._record_reload_event(outcome="rejected_parse", error=error)
            _LOG.warning("workflow reload failed: %s", error)
            return WorkflowReloadResult(
                current_snapshot=self.snapshot,
                changed=True,
                error=error,
                dispatch_paused=True,
            )

        new_snapshot = WorkflowSnapshot(
            workflow=workflow,
            config=workflow.config,
            workflow_path=workflow.path,
            metadata=metadata,
            revision=self.snapshot.revision + 1,
            loaded_at=self._clock(),
        )
        changed_fields = _changed_top_level_fields(self.snapshot.config, workflow.config)
        self.snapshot = new_snapshot
        self._record_reload_event(
            outcome="accepted",
            changed_fields=changed_fields,
            revision=new_snapshot.revision,
        )
        _LOG.info(
            "workflow reload accepted: revision=%d changed=%s",
            new_snapshot.revision,
            changed_fields,
        )
        return WorkflowReloadResult(
            current_snapshot=new_snapshot,
            changed=True,
            reloaded=True,
        )

    def _record_reload_event(
        self,
        *,
        outcome: str,
        changed_fields: list[str] | None = None,
        error: str | None = None,
        revision: int | None = None,
    ) -> None:
        root = self.snapshot.config.claude.artifact_store
        target = Path(root) / REPORT_DIR_NAME / "_reload_events.jsonl"
        payload = {
            "timestamp": self._clock().isoformat(),
            "outcome": outcome,
            "revision": revision if revision is not None else self.snapshot.revision,
            "workflow_path": (
                str(self.snapshot.workflow_path) if self.snapshot.workflow_path else None
            ),
            "changed_fields": changed_fields or [],
            "error": error,
        }
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        redact(payload, redact_keys=self.snapshot.config.logging.redact_keys),
                        sort_keys=True,
                    )
                    + "\n"
                )
        except OSError as exc:
            _LOG.warning("workflow reload failed to write event %s: %s", target, exc)


def _stat_metadata(path: Path) -> WorkflowFileMetadata:
    stat = path.stat()
    return WorkflowFileMetadata(
        mtime_ns=stat.st_mtime_ns,
        size=stat.st_size,
        inode=getattr(stat, "st_ino", None),
    )


def _restart_required_changes(old: WorkflowConfig, new: WorkflowConfig) -> list[str]:
    checks = {
        "tracker.kind": old.tracker.kind != new.tracker.kind,
        "tracker.owner": old.tracker.owner != new.tracker.owner,
        "tracker.repo": old.tracker.repo != new.tracker.repo,
        "tracker.token": old.tracker.token != new.tracker.token,
        "workspace.root": old.workspace.root != new.workspace.root,
        "claude.session_store": old.claude.session_store != new.claude.session_store,
        "claude.transcript_store": old.claude.transcript_store != new.claude.transcript_store,
        "claude.artifact_store": old.claude.artifact_store != new.claude.artifact_store,
        "claude.retry_resume_policy": (
            old.claude.retry_resume_policy != new.claude.retry_resume_policy
        ),
    }
    return [field for field, changed in checks.items() if changed]


def _changed_top_level_fields(old: WorkflowConfig, new: WorkflowConfig) -> list[str]:
    out: list[str] = []
    for field in (
        "tracker",
        "agent",
        "workspace",
        "claude",
        "github",
        "polling",
        "retry",
        "logging",
    ):
        if getattr(old, field) != getattr(new, field):
            out.append(field)
    return out


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


__all__ = [
    "WorkflowFileMetadata",
    "WorkflowReloader",
    "WorkflowReloadResult",
    "WorkflowSnapshot",
]
