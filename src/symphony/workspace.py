"""Per-issue workspace manager.

The orchestrator hands an :class:`~symphony.models.Issue` to
:meth:`WorkspaceManager.prepare`, gets back a :class:`~symphony.models.Workspace`
record, and uses :meth:`WorkspaceManager.run_hook` to execute the four
configured lifecycle hooks (``after_create``, ``before_run``, ``after_run``,
``before_delete``) before/after running the agent provider.

This module owns all path sanitization and root-containment checks defined
in ``SPEC.md`` §8. It does not perform git population — that boundary is
documented (`prepare` returns a `Workspace.repo_path == Workspace.path`)
and will be filled in by a later issue if needed.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from symphony.config import WorkspaceConfig
from symphony.models import Issue, Workspace

HookName = Literal["after_create", "before_run", "after_run", "before_delete"]

# SPEC §8: workspace names MUST only contain [A-Za-z0-9._-]; other characters
# are replaced with `_`. We additionally guard against pathological values
# (`..`, `.`, empty) below in :func:`_sanitize_component`.
_ALLOWED = re.compile(r"[A-Za-z0-9._-]")


# -- Errors --------------------------------------------------------------------


class WorkspaceError(ValueError):
    """Raised when a workspace cannot be prepared safely.

    Carries a path-style ``location`` (e.g. ``"workspace.path"``,
    ``"workspace.owner"``) so the CLI can surface operator-friendly
    messages without a Python traceback.
    """

    def __init__(self, location: str, message: str) -> None:
        super().__init__(f"{location}: {message}")
        self.location = location
        self.message = message


@dataclass(frozen=True, slots=True)
class HookResult:
    """Outcome of running a lifecycle hook.

    Distinguishes three cases the orchestrator and tests care about:
    ``returncode == 0`` (success), ``returncode != 0`` (non-zero exit),
    and ``timed_out`` (subprocess was killed after ``timeout_ms``). The
    method does not raise for non-zero exits — the orchestrator may
    choose to continue past a failed hook depending on the hook stage.

    On timeout, ``returncode`` is ``None`` (the subprocess was killed
    before it could produce one) and ``timed_out`` is True. Using
    ``None`` rather than a sentinel int avoids collisions with real
    Unix signal-derived returncodes (e.g. ``-1`` is ``-SIGHUP``).
    """

    name: HookName
    command: str
    returncode: int | None
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool

    @property
    def succeeded(self) -> bool:
        return self.returncode == 0 and not self.timed_out


# -- Helpers -------------------------------------------------------------------


def _sanitize_component(value: str, *, location: str) -> str:
    """Sanitize one path component (owner or repo) per SPEC §8.

    - Maps each disallowed character to ``_``.
    - Rejects empty strings and pathological values (``.``, ``..``, all dots)
      that would otherwise survive sanitization and let a callers escape
      the workspace root via ``../``.
    """
    if not value:
        raise WorkspaceError(location, "must not be empty")
    sanitized = "".join(ch if _ALLOWED.match(ch) else "_" for ch in value)
    # Reject path traversal and reserved single-/double-dot components even
    # though `.` is in the allowed set — `..` would resolve outside the
    # workspace root.
    if sanitized in {".", ".."} or set(sanitized) == {"."}:
        raise WorkspaceError(
            location,
            f"sanitized component {sanitized!r} is reserved and would escape root",
        )
    return sanitized


def _build_workspace_key(issue: Issue) -> str:
    owner = _sanitize_component(issue.owner, location="issue.owner")
    repo = _sanitize_component(issue.repo, location="issue.repo")
    if not isinstance(issue.number, int) or issue.number <= 0:
        raise WorkspaceError("issue.number", f"must be a positive int, got {issue.number!r}")
    return f"{owner}_{repo}_{issue.number}"


# -- Manager -------------------------------------------------------------------


class WorkspaceManager:
    """Prepares and operates on per-issue workspaces.

    The manager is stateless apart from the resolved
    :class:`~symphony.config.WorkspaceConfig` it was constructed with, so
    it is safe to share between concurrent orchestrator workers as long as
    each call targets a different :class:`~symphony.models.Issue`.

    The manager NEVER deletes an existing workspace from
    :meth:`prepare`; cleanup is opt-in via :meth:`delete` and is gated by
    the orchestrator's cleanup policy.
    """

    def __init__(self, config: WorkspaceConfig) -> None:
        self.config = config
        # Resolve once so all containment checks compare absolute paths.
        self._root = Path(config.root).resolve()

    @property
    def root(self) -> Path:
        return self._root

    # -- Path computation ---------------------------------------------------

    def workspace_path(self, issue: Issue) -> Path:
        """Compute the absolute workspace path for ``issue`` without I/O.

        Useful for restart recovery (the path is deterministic and stable
        across runs) and for tests that only want to check naming.
        """
        key = _build_workspace_key(issue)
        candidate = (self._root / key).resolve()
        # Defense in depth: even after sanitization, refuse to return a
        # path that is not strictly inside the configured root.
        if not candidate.is_relative_to(self._root):
            raise WorkspaceError(
                "workspace.path",
                f"resolved path {candidate} escapes workspace.root {self._root}",
            )
        if candidate == self._root:
            raise WorkspaceError(
                "workspace.path",
                "resolved path equals workspace.root (sanitization stripped to empty)",
            )
        return candidate

    # -- Lifecycle ----------------------------------------------------------

    def prepare(self, issue: Issue) -> Workspace:
        """Create or reuse the workspace directory for ``issue``.

        Existing directories are reused (``Workspace.reused == True``).
        Newly created directories get ``Workspace.reused == False``; the
        orchestrator uses that flag to decide whether to run the
        ``after_create`` hook.

        The workspace root is created on demand (``mkdir(parents=True)``)
        because forcing the operator to pre-create it adds friction with
        no safety benefit — the SPEC §8 root containment check already
        guarantees we cannot stray outside the configured location.
        """
        path = self.workspace_path(issue)
        # Ensure the configured root exists first; mkdir is idempotent.
        self._root.mkdir(parents=True, exist_ok=True)
        reused = path.exists()
        if not reused:
            path.mkdir(parents=True, exist_ok=False)
        elif not path.is_dir():
            # A file (not a directory) is parked at the workspace path —
            # do NOT clobber it. Surface the conflict to the operator.
            raise WorkspaceError(
                "workspace.path",
                f"path {path} exists but is not a directory; refusing to clobber",
            )
        return Workspace(
            issue_identifier=issue.identifier,
            workspace_key=path.name,
            path=path,
            repo_path=path,
            created_at=datetime.now(timezone.utc),
            reused=reused,
        )

    def delete(self, workspace: Workspace) -> None:
        """Delete the workspace directory.

        Refuses to delete a path that is not under the configured root —
        another defense against config-time mistakes. Raises if the path
        does not exist (caller should check `workspace.path.exists()` if
        idempotency is desired).
        """
        path = workspace.path.resolve()
        if not path.is_relative_to(self._root):
            raise WorkspaceError(
                "workspace.path",
                f"refuse to delete {path} which is outside workspace.root {self._root}",
            )
        if path == self._root:
            raise WorkspaceError(
                "workspace.path",
                "refuse to delete workspace.root itself",
            )
        if not path.exists():
            raise WorkspaceError("workspace.path", f"path {path} does not exist")
        shutil.rmtree(path)

    # -- Hooks --------------------------------------------------------------

    def run_hook(self, name: HookName, workspace: Workspace) -> HookResult | None:
        """Run the configured shell hook ``name`` in ``workspace.path``.

        Returns ``None`` if no hook is configured for ``name``. Otherwise
        returns a :class:`HookResult` with capture of stdout, stderr,
        return code, wallclock duration, and a ``timed_out`` flag. A
        non-zero exit does NOT raise — the orchestrator decides what to
        do with each stage's failure.

        ``cwd`` is ALWAYS the workspace path (per SPEC §8 / #6 acceptance
        criteria). If the workspace path is missing — including for
        ``before_delete``, which used to silently fall back to the
        Symphony process cwd — :class:`WorkspaceError` is raised so the
        orchestrator can decide whether to skip cleanup. Lifecycle shell
        commands are workspace-scoped by contract; running them in the
        wrong cwd is a safety bug.
        """
        command = self._hook_command(name)
        if command is None:
            return None
        if not workspace.path.exists():
            raise WorkspaceError(
                "workspace.path",
                f"workspace path {workspace.path} does not exist; cannot run hook {name!r}",
            )
        timeout_seconds = max(0.001, self.config.hook_timeout_ms / 1000)
        start = datetime.now(timezone.utc)
        timed_out = False
        try:
            completed = subprocess.run(  # noqa: S602 — shell intentional
                command,
                shell=True,
                cwd=str(workspace.path),
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
            )
            returncode = completed.returncode
            stdout = completed.stdout
            stderr = completed.stderr
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            # None (not -1, which would collide with -SIGHUP) so callers
            # can rely on `timed_out` as the source of truth.
            returncode = None
            stdout = _decode_partial(exc.stdout)
            stderr = _decode_partial(exc.stderr)
        duration_ms = int((datetime.now(timezone.utc) - start).total_seconds() * 1000)
        return HookResult(
            name=name,
            command=command,
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
            duration_ms=duration_ms,
            timed_out=timed_out,
        )

    def _hook_command(self, name: HookName) -> str | None:
        return getattr(self.config, name)


# -- Module-level helpers -----------------------------------------------------


def _decode_partial(data: bytes | str | None) -> str:
    """Decode the partial output captured before a subprocess timeout.

    ``subprocess.TimeoutExpired.stdout/stderr`` is bytes when ``capture_output``
    is set but text-mode buffering was incomplete; sometimes it is already
    str (text=True path), sometimes None. This helper hides the variance.
    """
    if data is None:
        return ""
    if isinstance(data, bytes):
        return data.decode("utf-8", errors="replace")
    return data
