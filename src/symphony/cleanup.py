"""Workspace cleanup executor (#66, M5.7).

Consumes the M5.6 schema (``workspace.cleanup`` from #65 / PR #78) and
deletes per-issue workspaces under three trigger conditions:

- ``on_terminal_issue`` — fires from the orchestrator's worker finally
  block when a worker reaches a clean terminal task outcome.
- ``on_closed_pr`` — fires from reconciliation when a worker's linked
  PR is closed/merged.
- ``max_age_days`` — fires from a sweep at the top of each
  :meth:`Orchestrator.run_once`, walking ``workspace.root`` and
  deleting per-issue workspaces older than the threshold (skips
  workspaces currently held by an active worker).

Safety guards (all defended by tests):

- Cleanup is **disabled by default** — ``cleanup.enabled=False`` (the
  M5.6 default) bypasses every method here. Existing workflows
  preserve workspaces unchanged.
- The path-safety check delegates to
  :meth:`WorkspaceManager.delete`, which already refuses to delete
  paths outside ``workspace.root`` or to delete ``workspace.root``
  itself. This module never bypasses that boundary.
- ``dry_run=true`` reports the intended deletion (returned in
  :class:`CleanupDecision`) and emits a WARNING log line, but does
  NOT delete.
- Missing target paths are handled idempotently — a workspace already
  deleted by a prior tick or by an operator returns
  :data:`CleanupAction.KEPT_NOT_FOUND` rather than raising.

The executor is intentionally agnostic of the orchestrator's
internal state: each method takes the inputs it needs (a
:class:`Workspace`, an active-identifier set, etc.) and returns a
:class:`CleanupDecision`. The orchestrator is responsible for
threading the decisions into ``terminal.json`` / artifact streams
once #67 ships the reporting surface.
"""

from __future__ import annotations

import enum
import logging
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from symphony.config import WorkspaceCleanupConfig
from symphony.models import Workspace
from symphony.workspace import WorkspaceError, WorkspaceManager

_LOG = logging.getLogger("symphony.cleanup")


# -- Decision surface --------------------------------------------------------


class CleanupAction(str, enum.Enum):
    """Why the executor did (or did not) delete a workspace.

    Concrete enum so logs/artifacts can show a stable string. The
    string form mirrors what an operator would grep for.
    """

    DELETED = "deleted"
    KEPT_DRY_RUN = "kept_dry_run"
    KEPT_DISABLED = "kept_disabled"
    KEPT_TRIGGER_NOT_SET = "kept_trigger_not_set"
    KEPT_ACTIVE = "kept_active"
    KEPT_OUTSIDE_ROOT = "kept_outside_root"
    KEPT_NOT_FOUND = "kept_not_found"
    KEPT_TOO_YOUNG = "kept_too_young"
    KEPT_PR_OPEN = "kept_pr_open"
    KEPT_ERROR = "kept_error"


@dataclass(frozen=True, slots=True)
class CleanupDecision:
    """Outcome of one cleanup attempt.

    The orchestrator will eventually thread this into ``terminal.json``
    / a per-tick cleanup report (#67). For now it stays local to the
    executor and is logged for operator visibility.
    """

    workspace_path: Path
    action: CleanupAction
    reason: str
    trigger: str  # one of "terminal_issue" / "closed_pr" / "age" / "manual"
    decided_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


# -- Executor ----------------------------------------------------------------


class WorkspaceCleanupExecutor:
    """Per-orchestrator cleanup driver.

    Construction takes the existing :class:`WorkspaceManager` (so we
    inherit its path-safety guards) plus the resolved
    :class:`WorkspaceCleanupConfig` from the workflow. Tests can
    inject a manager wired to a tmp_path root.
    """

    def __init__(
        self,
        workspace_manager: WorkspaceManager,
        config: WorkspaceCleanupConfig,
        *,
        clock: object | None = None,
    ) -> None:
        self._mgr = workspace_manager
        self._config = config
        self._clock = clock or _now_utc

    # -- Trigger entrypoints --------------------------------------------------

    def cleanup_for_terminal_issue(
        self, workspace: Workspace
    ) -> CleanupDecision:
        """Hook called from the orchestrator worker finally block.

        Fires only when the run reached a clean terminal task outcome
        AND ``workspace.cleanup.enabled`` AND ``on_terminal_issue``.
        Anything else returns a `KEPT_*` decision without touching disk.
        """
        if not self._config.enabled:
            return self._kept(
                workspace.path, CleanupAction.KEPT_DISABLED, "cleanup disabled", "terminal_issue"
            )
        if not self._config.on_terminal_issue:
            return self._kept(
                workspace.path,
                CleanupAction.KEPT_TRIGGER_NOT_SET,
                "on_terminal_issue=false",
                "terminal_issue",
            )
        return self._delete(workspace.path, "terminal_issue", "terminal task outcome")

    def cleanup_for_closed_pr(
        self, workspace: Workspace, *, pr_state: str
    ) -> CleanupDecision:
        """Hook called from reconciliation when a worker's linked PR
        is closed/merged. ``pr_state`` is the GitHub state string
        (`"closed"` / `"merged"`); anything else short-circuits."""
        if not self._config.enabled:
            return self._kept(
                workspace.path, CleanupAction.KEPT_DISABLED, "cleanup disabled", "closed_pr"
            )
        if not self._config.on_closed_pr:
            return self._kept(
                workspace.path,
                CleanupAction.KEPT_TRIGGER_NOT_SET,
                "on_closed_pr=false",
                "closed_pr",
            )
        if pr_state not in {"closed", "merged"}:
            return self._kept(
                workspace.path,
                CleanupAction.KEPT_PR_OPEN,
                f"pr_state={pr_state!r}",
                "closed_pr",
            )
        return self._delete(workspace.path, "closed_pr", f"linked PR {pr_state}")

    def sweep_for_age(
        self, *, active_identifiers: set[str] | None = None
    ) -> list[CleanupDecision]:
        """Walk ``workspace.root`` and delete per-issue workspaces older
        than ``max_age_days``. ``active_identifiers`` are the issue
        identifiers (``owner/repo#N``) currently held by orchestrator
        workers — those workspaces are NEVER deleted regardless of age.

        Returns one :class:`CleanupDecision` per workspace inspected,
        whether deleted or kept. Returns an empty list when cleanup is
        disabled or ``max_age_days`` is unset.
        """
        if not self._config.enabled or self._config.max_age_days is None:
            return []
        active = active_identifiers or set()
        # Pre-build the active workspace key set so we can compare with
        # `Path.name` without re-deriving owner/repo/number.
        active_keys = {_workspace_key_from_identifier(i) for i in active}
        decisions: list[CleanupDecision] = []
        root = self._mgr.root
        if not root.is_dir():
            return decisions
        cutoff = self._clock() - _seconds(days=self._config.max_age_days)
        for entry in sorted(root.iterdir()):
            if not entry.is_dir():
                continue
            if entry.name in active_keys:
                decisions.append(
                    self._kept(
                        entry,
                        CleanupAction.KEPT_ACTIVE,
                        f"workspace key {entry.name!r} held by active worker",
                        "age",
                    )
                )
                continue
            try:
                mtime = datetime.fromtimestamp(entry.stat().st_mtime, tz=timezone.utc)
            except OSError as exc:
                decisions.append(
                    self._kept(
                        entry,
                        CleanupAction.KEPT_ERROR,
                        f"could not stat {entry}: {exc}",
                        "age",
                    )
                )
                continue
            if mtime > cutoff:
                decisions.append(
                    self._kept(
                        entry,
                        CleanupAction.KEPT_TOO_YOUNG,
                        (
                            f"mtime={mtime.isoformat()} newer than cutoff={cutoff.isoformat()} "
                            f"(max_age_days={self._config.max_age_days})"
                        ),
                        "age",
                    )
                )
                continue
            decisions.append(
                self._delete(
                    entry,
                    "age",
                    (
                        f"older than {self._config.max_age_days} days "
                        f"(mtime={mtime.isoformat()})"
                    ),
                )
            )
        return decisions

    # -- Internals -----------------------------------------------------------

    def _delete(self, path: Path, trigger: str, reason: str) -> CleanupDecision:
        """Apply the path-safety check + dry_run gate + idempotent removal."""
        if not path.exists():
            decision = self._kept(
                path,
                CleanupAction.KEPT_NOT_FOUND,
                f"path {path} already missing (idempotent)",
                trigger,
            )
            return decision
        # Defer to the existing safety guards on WorkspaceManager.delete:
        # it refuses outside-root and root-itself paths. We can't call
        # delete() directly because it requires a Workspace dataclass;
        # the safety check is duplicated here against `self._mgr.root`
        # so the executor is self-contained.
        try:
            resolved = path.resolve()
        except OSError as exc:
            return self._kept(
                path,
                CleanupAction.KEPT_ERROR,
                f"could not resolve {path}: {exc}",
                trigger,
            )
        root = self._mgr.root
        if not resolved.is_relative_to(root):
            return self._kept(
                path,
                CleanupAction.KEPT_OUTSIDE_ROOT,
                f"refuse to delete {resolved} which is outside workspace.root {root}",
                trigger,
            )
        if resolved == root:
            return self._kept(
                path,
                CleanupAction.KEPT_OUTSIDE_ROOT,
                f"refuse to delete workspace.root {root} itself",
                trigger,
            )
        if self._config.dry_run:
            _LOG.warning(
                "cleanup dry_run: would delete %s (trigger=%s, reason=%s)",
                resolved,
                trigger,
                reason,
            )
            return CleanupDecision(
                workspace_path=resolved,
                action=CleanupAction.KEPT_DRY_RUN,
                reason=reason,
                trigger=trigger,
            )
        try:
            shutil.rmtree(resolved)
        except OSError as exc:
            return self._kept(
                path,
                CleanupAction.KEPT_ERROR,
                f"rmtree({resolved}) failed: {exc}",
                trigger,
            )
        _LOG.info(
            "cleanup deleted workspace %s (trigger=%s, reason=%s)",
            resolved,
            trigger,
            reason,
        )
        return CleanupDecision(
            workspace_path=resolved,
            action=CleanupAction.DELETED,
            reason=reason,
            trigger=trigger,
        )

    @staticmethod
    def _kept(
        path: Path, action: CleanupAction, reason: str, trigger: str
    ) -> CleanupDecision:
        return CleanupDecision(
            workspace_path=path, action=action, reason=reason, trigger=trigger
        )


# -- Module helpers ----------------------------------------------------------


def _workspace_key_from_identifier(identifier: str) -> str:
    """Translate ``owner/repo#N`` → ``owner_repo_N`` (the workspace key).

    Mirrors :func:`symphony.workspace._build_workspace_key`'s output
    without re-importing the underscore-prefixed helper. Tests for the
    sweep verify both forms produce the same key.
    """
    # ``owner/repo#N`` → ``owner_repo_N``
    if "#" not in identifier:
        return identifier.replace("/", "_")
    issue_part, number = identifier.split("#", 1)
    owner, _, repo = issue_part.partition("/")
    return f"{owner}_{repo}_{number}"


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _seconds(*, days: int) -> timedelta:
    return timedelta(days=days)


# Suppress unused-import warning when WorkspaceError isn't actually
# raised here — kept in scope so the module's documented relationship
# with WorkspaceManager.delete's safety guards is greppable.
_unused = (WorkspaceError,)


__all__ = [
    "CleanupAction",
    "CleanupDecision",
    "WorkspaceCleanupExecutor",
]
