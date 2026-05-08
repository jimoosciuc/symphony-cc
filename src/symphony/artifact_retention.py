"""Artifact retention executor (#67, M5.8).

Consumes ``claude.artifact_retention``. Artifacts are audit evidence, so
retention is disabled by default and only age-based deletion is supported
by the M5.6 schema.
"""

from __future__ import annotations

import json
import logging
import shutil
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from symphony.artifacts import redact
from symphony.config import ArtifactRetentionConfig

_LOG = logging.getLogger("symphony.artifact_retention")

REPORT_DIR_NAME = "_retention_reports"


@dataclass(frozen=True, slots=True)
class ArtifactRetentionDecision:
    path: Path
    action: str
    reason: str
    mtime: datetime | None = None


@dataclass(frozen=True, slots=True)
class ArtifactRetentionReport:
    artifact_store: Path
    dry_run: bool
    enabled: bool
    max_age_days: int | None
    cutoff: datetime | None
    decided_at: datetime
    decisions: list[ArtifactRetentionDecision]

    @property
    def considered(self) -> int:
        return len(self.decisions)

    @property
    def deleted(self) -> int:
        return sum(1 for d in self.decisions if d.action == "deleted")

    @property
    def skipped(self) -> int:
        return sum(1 for d in self.decisions if d.action.startswith("skipped"))

    @property
    def errors(self) -> int:
        return sum(1 for d in self.decisions if d.action == "error")


class ArtifactRetentionExecutor:
    """Age-based cleanup for ``claude.artifact_store`` attempt dirs."""

    def __init__(
        self,
        artifact_store: Path,
        config: ArtifactRetentionConfig,
        *,
        redact_keys: tuple[str, ...],
        clock: object | None = None,
    ) -> None:
        self._root = Path(artifact_store).resolve()
        self._config = config
        self._redact_keys = redact_keys
        self._clock = clock or _now_utc

    def sweep(self) -> ArtifactRetentionReport | None:
        if not self._config.enabled:
            return None
        now = self._clock()
        cutoff = now - timedelta(days=self._config.max_age_days or 0)
        decisions: list[ArtifactRetentionDecision] = []
        if not self._root.is_dir():
            report = ArtifactRetentionReport(
                artifact_store=self._root,
                dry_run=self._config.dry_run,
                enabled=True,
                max_age_days=self._config.max_age_days,
                cutoff=cutoff,
                decided_at=now,
                decisions=[],
            )
            self._write_report(report)
            return report

        for attempt_dir in self._iter_attempt_dirs():
            decisions.append(self._decide_attempt(attempt_dir, cutoff=cutoff))

        report = ArtifactRetentionReport(
            artifact_store=self._root,
            dry_run=self._config.dry_run,
            enabled=True,
            max_age_days=self._config.max_age_days,
            cutoff=cutoff,
            decided_at=now,
            decisions=decisions,
        )
        self._write_report(report)
        return report

    def _iter_attempt_dirs(self) -> list[Path]:
        out: list[Path] = []
        for issue_dir in sorted(self._root.iterdir()):
            if not issue_dir.is_dir() or issue_dir.name == REPORT_DIR_NAME:
                continue
            for attempt_dir in sorted(issue_dir.iterdir()):
                if attempt_dir.is_dir():
                    out.append(attempt_dir)
        return out

    def _decide_attempt(self, path: Path, *, cutoff: datetime) -> ArtifactRetentionDecision:
        try:
            resolved = path.resolve()
        except OSError as exc:
            return ArtifactRetentionDecision(
                path=path,
                action="error",
                reason=f"resolve failed: {exc}",
            )
        if not resolved.is_relative_to(self._root) or resolved == self._root:
            return ArtifactRetentionDecision(
                path=path,
                action="skipped_outside_root",
                reason=f"refuse to delete {resolved} outside artifact_store {self._root}",
            )
        try:
            mtime = datetime.fromtimestamp(resolved.stat().st_mtime, tz=timezone.utc)
        except OSError as exc:
            return ArtifactRetentionDecision(
                path=resolved,
                action="error",
                reason=f"stat failed: {exc}",
            )
        if mtime > cutoff:
            return ArtifactRetentionDecision(
                path=resolved,
                action="skipped_too_young",
                reason=f"mtime={mtime.isoformat()} newer than cutoff={cutoff.isoformat()}",
                mtime=mtime,
            )
        if self._config.dry_run:
            _LOG.warning("artifact retention dry_run: would delete %s", resolved)
            return ArtifactRetentionDecision(
                path=resolved,
                action="skipped_dry_run",
                reason="dry-run: artifact attempt would be deleted",
                mtime=mtime,
            )
        try:
            shutil.rmtree(resolved)
        except OSError as exc:
            _LOG.warning("artifact retention failed to delete %s: %s", resolved, exc)
            return ArtifactRetentionDecision(
                path=resolved,
                action="error",
                reason=f"rmtree failed: {exc}",
                mtime=mtime,
            )
        _LOG.info("artifact retention deleted %s", resolved)
        return ArtifactRetentionDecision(
            path=resolved,
            action="deleted",
            reason=f"older than {self._config.max_age_days} days",
            mtime=mtime,
        )

    def _write_report(self, report: ArtifactRetentionReport) -> Path:
        report_dir = self._root / REPORT_DIR_NAME
        report_dir.mkdir(parents=True, exist_ok=True)
        stamp = report.decided_at.strftime("%Y%m%dT%H%M%SZ")
        target = self._next_report_path(report_dir, stamp)
        payload = asdict(report)
        payload["artifact_store"] = str(report.artifact_store)
        payload["cutoff"] = report.cutoff.isoformat() if report.cutoff else None
        payload["decided_at"] = report.decided_at.isoformat()
        payload["summary"] = {
            "considered": report.considered,
            "deleted": report.deleted,
            "skipped": report.skipped,
            "errors": report.errors,
        }
        for decision in payload["decisions"]:
            decision["path"] = str(decision["path"])
            if decision["mtime"] is not None:
                decision["mtime"] = decision["mtime"].isoformat()
        redacted = redact(payload, redact_keys=self._redact_keys)
        target.write_text(json.dumps(redacted, indent=2, sort_keys=True), encoding="utf-8")
        return target

    def _next_report_path(self, report_dir: Path, stamp: str) -> Path:
        target = report_dir / f"artifact-retention-{stamp}.json"
        if not target.exists():
            return target
        index = 2
        while True:
            candidate = report_dir / f"artifact-retention-{stamp}-{index}.json"
            if not candidate.exists():
                return candidate
            index += 1


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


__all__ = [
    "ArtifactRetentionDecision",
    "ArtifactRetentionExecutor",
    "ArtifactRetentionReport",
    "REPORT_DIR_NAME",
]
