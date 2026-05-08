"""Per-issue workspace manager.

The orchestrator hands an :class:`~symphony.models.Issue` to
:meth:`WorkspaceManager.prepare`, gets back a :class:`~symphony.models.Workspace`
record, and uses :meth:`WorkspaceManager.run_hook` to execute the four
configured lifecycle hooks (``after_create``, ``before_run``, ``after_run``,
``before_delete``) before/after running the agent provider.

This module owns all path sanitization and root-containment checks defined
in ``SPEC.md`` §8. Repository population (``workspace.populate: git``) is
delegated to a :class:`WorkspacePopulator` injected at construction time —
when present, ``prepare`` calls it after creating/reusing the workspace
directory. Production wires :class:`GitWorkspacePopulator`; tests can
inject a stub or leave it ``None`` (the default), in which case ``prepare``
keeps its prior empty-directory behavior.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Protocol

from symphony.config import GitHubConfig, TrackerConfig, WorkspaceConfig
from symphony.models import Issue, Workspace

_LOG = logging.getLogger("symphony.workspace")

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


def workspace_key_from_identifier(identifier: str) -> str:
    """Translate ``owner/repo#N`` → ``owner_repo_N`` (the workspace key).

    Public counterpart to :func:`_build_workspace_key` that operates on
    the string identifier instead of a full :class:`Issue`. Symphony's
    cleanup executor (#66) needs to derive the on-disk workspace key
    from active workers' identifiers without rebuilding `Issue` objects;
    sharing the format with this single helper keeps the two callers
    in lockstep so a future workspace-naming change can't desync them.

    Defensive on shape: a missing ``#N`` separator falls back to a
    flat ``owner_repo`` mapping (no crash) — used by tests that
    pass non-issue identifiers through the cleanup sweep filter.
    """
    if "#" not in identifier:
        return identifier.replace("/", "_")
    issue_part, number = identifier.split("#", 1)
    owner, _, repo = issue_part.partition("/")
    return f"{owner}_{repo}_{number}"


# -- Manager -------------------------------------------------------------------


class WorkspacePopulator(Protocol):
    """Boundary for filling a freshly-created or reused workspace directory.

    The manager owns path safety; the populator owns whatever
    ``workspace.populate`` strategy is configured. Implementations MUST
    be safe to call on both fresh (empty) and reused (post-prior-run)
    directories — ``prepare`` does not differentiate.
    """

    def populate(self, workspace_path: Path, issue: Issue, *, reused: bool) -> None: ...


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

    def __init__(
        self,
        config: WorkspaceConfig,
        *,
        populator: WorkspacePopulator | None = None,
    ) -> None:
        self.config = config
        # Resolve once so all containment checks compare absolute paths.
        self._root = Path(config.root).resolve()
        self._populator = populator

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
        # Run the configured population strategy (e.g., git clone/fetch+reset)
        # AFTER the directory exists so the populator can assume an empty or
        # previously-populated dir. ``populate: git`` without a populator wired
        # is a no-op so existing tests that construct ``WorkspaceManager``
        # without one keep working.
        if self.config.populate == "git" and self._populator is not None:
            self._populator.populate(path, issue, reused=reused)
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


# -- Git populator (#44) ------------------------------------------------------


GITHUB_HTTPS_HOST = "github.com"


class GitWorkspacePopulator:
    """Populate a workspace via ``git clone`` / ``git fetch`` (#44).

    Wired by the CLI when ``workspace.populate: git`` is configured. The
    populator owns:

    - building the authenticated remote URL (token is passed via the URL
      only at clone time, then scrubbed from ``.git/config`` so it does
      not survive into the repo or operator artifacts);
    - choosing between fresh clone (empty / no ``.git`` dir) and reuse
      (fetch + reset to ``github.base_branch`` + clean untracked);
    - raising :class:`WorkspaceError` on any git failure so the
      orchestrator can mark the issue blocked instead of running Claude
      against an empty / inconsistent workspace.

    The token is sourced from :class:`TrackerConfig.token`; everything
    Claude sees (the workspace's ``.git/config`` and any subsequent
    ``git remote -v``) carries only the unauthenticated HTTPS URL.

    Operator-side caveats (matching SPEC §8 reuse semantics):

    - **Reuse is destructive of local state.** ``git fetch`` + ``git
      reset --hard origin/<base>`` + ``git clean -fdx`` discards any
      uncommitted edits the prior session made. That is the contract:
      every run starts from a deterministic checkout of the base
      branch. If an operator wants to inspect mid-run state, do it
      before the next dispatch tick.
    - The populator does NOT push. PR creation is agent-managed
      (per ``docs/claude-provider.md``).
    """

    # Network operations get their own timeout so a hung clone does not
    # block the orchestrator's poll loop. Workspace hook timeout would be
    # too generous for an HTTPS clone of a small repo.
    DEFAULT_TIMEOUT_SECONDS: float = 300.0

    def __init__(
        self,
        tracker: TrackerConfig,
        github: GitHubConfig,
        *,
        timeout_seconds: float | None = None,
        host: str = GITHUB_HTTPS_HOST,
    ) -> None:
        self._tracker = tracker
        self._github = github
        self._timeout_seconds = timeout_seconds or self.DEFAULT_TIMEOUT_SECONDS
        self._host = host

    # -- Public surface ------------------------------------------------------

    def populate(self, workspace_path: Path, issue: Issue, *, reused: bool) -> None:
        """Populate ``workspace_path`` with a checkout of the configured repo.

        Strategy:

        - If a ``.git`` directory exists at ``workspace_path`` (regardless
          of whether the manager flagged the dir as ``reused``), refresh
          via ``fetch`` + ``reset`` + ``clean``. Defends against the case
          where the manager reuses a directory operator-cloned manually.
        - Otherwise, clone the configured repo into ``workspace_path``.

        Raises :class:`WorkspaceError` (location ``workspace.populate``)
        on any failure — operator-facing message, no token in it.
        """
        del reused  # presence of .git is the source of truth, not the manager flag
        if (workspace_path / ".git").is_dir():
            self._refresh_existing(workspace_path)
        else:
            self._fresh_clone(workspace_path)

    # -- Internals -----------------------------------------------------------

    def _fresh_clone(self, workspace_path: Path) -> None:
        if any(workspace_path.iterdir()):
            # Non-empty without .git is operator state we should not stomp.
            raise WorkspaceError(
                "workspace.populate",
                (
                    f"directory {workspace_path} is non-empty but has no .git; "
                    "refusing to clobber operator state. Remove the directory "
                    "or migrate it to a real git checkout."
                ),
            )
        authed_url = self._authenticated_url()
        public_url = self._public_url()
        # ``git clone <url> .`` clones into the existing (empty) dir without
        # creating a nested directory.
        self._run_git(
            ["clone", "--branch", self._github.base_branch, authed_url, "."],
            cwd=workspace_path,
            secret=self._tracker.token,
        )
        # Scrub the token from the persisted remote so subsequent ``git
        # remote -v`` / ``.git/config`` reads from Claude or operator
        # tooling never see it. The local credential helper for *this*
        # checkout is intentionally not touched — there is none unless
        # the operator added one.
        self._run_git(
            ["remote", "set-url", "origin", public_url],
            cwd=workspace_path,
            secret=self._tracker.token,
        )

    def _refresh_existing(self, workspace_path: Path) -> None:
        authed_url = self._authenticated_url()
        public_url = self._public_url()
        # Set the remote to the authed URL only for the fetch, then scrub.
        # Could be done with ``git -c http.extraheader=...`` but that
        # leaks the token into the command line (visible via ``ps``);
        # the temporary remote URL keeps it out of process listings.
        self._run_git(
            ["remote", "set-url", "origin", authed_url],
            cwd=workspace_path,
            secret=self._tracker.token,
        )
        try:
            self._run_git(
                ["fetch", "--prune", "origin", self._github.base_branch],
                cwd=workspace_path,
                secret=self._tracker.token,
            )
        finally:
            # Even if fetch failed, restore the public URL so the token
            # does not stay in .git/config across runs.
            self._run_git(
                ["remote", "set-url", "origin", public_url],
                cwd=workspace_path,
                secret=self._tracker.token,
            )
        self._run_git(
            ["reset", "--hard", f"origin/{self._github.base_branch}"],
            cwd=workspace_path,
            secret=self._tracker.token,
        )
        self._run_git(
            ["clean", "-fdx"],
            cwd=workspace_path,
            secret=self._tracker.token,
        )

    def _authenticated_url(self) -> str:
        # GitHub PATs auth as ``oauth2:<token>``; installation tokens use
        # ``x-access-token:<token>``. Both accept ``oauth2`` for clone.
        # The token is URL-encoded defensively — PATs are alphanumeric
        # today, but a future format with reserved characters would
        # otherwise break the URL silently.
        from urllib.parse import quote

        token = quote(self._tracker.token, safe="")
        return (
            f"https://oauth2:{token}@{self._host}/"
            f"{self._tracker.owner}/{self._tracker.repo}.git"
        )

    def _public_url(self) -> str:
        return f"https://{self._host}/{self._tracker.owner}/{self._tracker.repo}.git"

    def _run_git(
        self,
        argv: list[str],
        *,
        cwd: Path,
        secret: str,
    ) -> None:
        """Run ``git`` with the supplied args; raise WorkspaceError on failure.

        Suppresses interactive credential prompts (so a misconfigured
        token fails fast instead of hanging the daemon). Strips the
        token from any captured stderr before raising — operator logs
        and CLI surfaces never carry the secret.
        """
        env = dict(os.environ)
        # Prevent SSH/HTTPS prompts from blocking the daemon.
        env.setdefault("GIT_TERMINAL_PROMPT", "0")
        env.setdefault("GIT_ASKPASS", "/bin/echo")
        try:
            completed = subprocess.run(  # noqa: S603 — argv list, no shell
                ["git", *argv],
                cwd=str(cwd),
                env=env,
                capture_output=True,
                text=True,
                timeout=self._timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise WorkspaceError(
                "workspace.populate",
                f"git {argv[0]} timed out after {self._timeout_seconds}s: {exc}",
            ) from exc
        except OSError as exc:
            raise WorkspaceError(
                "workspace.populate",
                f"git {argv[0]} could not be invoked ({exc.__class__.__name__}: {exc})",
            ) from exc
        if completed.returncode != 0:
            stderr = _scrub_secret(completed.stderr or "", secret)
            stdout = _scrub_secret(completed.stdout or "", secret)
            raise WorkspaceError(
                "workspace.populate",
                (
                    f"git {argv[0]} failed (exit {completed.returncode}). "
                    f"stderr: {stderr.strip()[:500]}"
                    + (f" | stdout: {stdout.strip()[:200]}" if stdout.strip() else "")
                ),
            )
        _LOG.debug("git %s succeeded in %s", argv[0], cwd)


def _scrub_secret(text: str, secret: str) -> str:
    """Remove the literal token (and its URL-encoded form) from ``text``.

    Defense in depth: git output should not echo the token via the
    authenticated URL because we never put the token on the command
    line, but a future code path that does (or a git version that
    quotes the URL into an error message) would otherwise leak.
    """
    if not secret:
        return text
    from urllib.parse import quote

    cleaned = text.replace(secret, "<redacted>")
    encoded = quote(secret, safe="")
    if encoded != secret:
        cleaned = cleaned.replace(encoded, "<redacted>")
    return cleaned
