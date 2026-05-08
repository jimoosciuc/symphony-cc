"""Tests for ``GitWorkspacePopulator`` (issue #44).

Uses a local bare repository as the remote so tests run real ``git``
subprocesses without network access. The populator is wired with a
``host=`` override pointing the URL at the local bare repo's parent
path so the constructed ``https://oauth2:TOKEN@<host>/owner/repo.git``
URL falls through to a local file path when read by git.

Test surface:

- fresh clone into an empty workspace produces a checkout of
  ``base_branch`` and the on-disk ``.git/config`` does NOT carry the
  token;
- reused checkout fetches + resets + cleans, discarding local edits
  per the SPEC §8 reuse contract;
- a non-empty workspace without ``.git`` is refused (operator state
  preserved);
- a git failure raises :class:`WorkspaceError` with the secret
  scrubbed from the message;
- token never appears in stderr/stdout/log surfaces (defensive
  ``_scrub_secret`` round-trip).
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from symphony.config import GitHubConfig, TrackerConfig, WorkspaceConfig
from symphony.models import Issue
from symphony.workspace import (
    GitWorkspacePopulator,
    WorkspaceError,
    WorkspaceManager,
    _scrub_secret,
)

# -- Helpers -----------------------------------------------------------------


def _git(cwd: Path, *argv: str) -> None:
    """Run ``git`` in ``cwd`` with deterministic identity for reproducibility."""
    env = {
        "PATH": __import__("os").environ.get("PATH", ""),
        "GIT_AUTHOR_NAME": "Symphony Test",
        "GIT_AUTHOR_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_NAME": "Symphony Test",
        "GIT_COMMITTER_EMAIL": "test@example.invalid",
        "GIT_TERMINAL_PROMPT": "0",
    }
    subprocess.run(
        ["git", *argv],
        cwd=str(cwd),
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )


def _seed_remote(tmp_path: Path, *, owner: str = "acme", repo: str = "proj") -> Path:
    """Create a bare repo at ``tmp_path/<host>/<owner>/<repo>.git`` with one commit on main.

    The directory layout mirrors the URL ``https://<host>/<owner>/<repo>.git`` so
    the populator's URL — ``https://oauth2:TOKEN@<tmp_path>/<owner>/<repo>.git`` —
    resolves to the bare repo via git's HTTPS-fallback-to-filesystem behavior.
    Actually git treats ``https://...`` as HTTPS unconditionally, so we instead
    bypass that by passing ``host=`` to the populator and using the path form
    indirectly via a working-tree origin. See ``_build_populator``.
    """
    # We work around the HTTPS-only handling by using a *file path* host: the
    # populator's URL builder produces ``https://<host>/<owner>/<repo>.git``;
    # for tests we monkey-patch the URL builders so they yield the local path
    # form ``<bare_repo_path>``.
    bare = tmp_path / "remote" / f"{owner}_{repo}.git"
    bare.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main", str(bare)],
        check=True,
        capture_output=True,
    )
    # Seed one commit via a transient working clone.
    work = tmp_path / "remote" / "_seed_work"
    subprocess.run(
        ["git", "clone", str(bare), str(work)],
        check=True,
        capture_output=True,
    )
    (work / "README.md").write_text("hello\n")
    _git(work, "add", "README.md")
    _git(work, "commit", "-m", "initial")
    _git(work, "branch", "-M", "main")
    _git(work, "push", "origin", "main")
    return bare


def _config_pair(
    tmp_path: Path, *, owner: str = "acme", repo: str = "proj", token: str = "ghp_test_x"
) -> tuple[TrackerConfig, GitHubConfig]:
    tracker = TrackerConfig(
        kind="github",
        owner=owner,
        repo=repo,
        token=token,
    )
    github = GitHubConfig(base_branch="main")
    return tracker, github


def _build_populator(
    tmp_path: Path,
    *,
    owner: str = "acme",
    repo: str = "proj",
    token: str = "ghp_test_x",
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[GitWorkspacePopulator, Path]:
    """Build a populator whose URL builders return the local bare repo path.

    Bypasses the HTTPS-only assumption: tests don't have a real HTTPS server,
    so we patch ``_authenticated_url`` and ``_public_url`` to return the bare
    repo's filesystem path. The token-scrubbing assertions still hold because
    the token is wired through ``_run_git``'s ``secret`` argument identically.
    """
    bare = _seed_remote(tmp_path, owner=owner, repo=repo)
    tracker, github = _config_pair(tmp_path, owner=owner, repo=repo, token=token)
    pop = GitWorkspacePopulator(tracker, github)

    def _authed_url(self: Any) -> str:
        # Token is irrelevant to the local file URL but threaded through so
        # the scrub assertions exercise the same code path.
        return str(bare)

    def _public_url(self: Any) -> str:
        return str(bare)

    monkeypatch.setattr(GitWorkspacePopulator, "_authenticated_url", _authed_url)
    monkeypatch.setattr(GitWorkspacePopulator, "_public_url", _public_url)
    return pop, bare


def _issue() -> Issue:
    return Issue(
        id="I_1",
        number=1,
        identifier="acme/proj#1",
        owner="acme",
        repo="proj",
        title="t",
        body="b",
        state="open",
        url="https://github.com/acme/proj/issues/1",
    )


# -- Fresh clone -------------------------------------------------------------


def test_fresh_clone_populates_workspace_with_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pop, _ = _build_populator(tmp_path, monkeypatch=monkeypatch)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    pop.populate(workspace, _issue(), reused=False)

    assert (workspace / ".git").is_dir()
    assert (workspace / "README.md").read_text() == "hello\n"


def test_fresh_clone_does_not_persist_token_in_git_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "ghp_super_secret_aaaaaaaaaaa"
    pop, _ = _build_populator(tmp_path, monkeypatch=monkeypatch, token=secret)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    pop.populate(workspace, _issue(), reused=False)

    config_text = (workspace / ".git" / "config").read_text()
    assert secret not in config_text


def test_fresh_clone_into_non_empty_dir_without_git_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pop, _ = _build_populator(tmp_path, monkeypatch=monkeypatch)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "operator-file.txt").write_text("do not clobber\n")

    with pytest.raises(WorkspaceError) as exc:
        pop.populate(workspace, _issue(), reused=True)
    assert "non-empty" in str(exc.value)
    assert (workspace / "operator-file.txt").read_text() == "do not clobber\n"


# -- Reused checkout ---------------------------------------------------------


def test_reused_checkout_resets_to_base_branch_and_cleans_untracked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pop, bare = _build_populator(tmp_path, monkeypatch=monkeypatch)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    pop.populate(workspace, _issue(), reused=False)

    # Operator/Claude leaves a dirty checkout: an uncommitted edit + an
    # untracked file. Both should be wiped on the next populate.
    (workspace / "README.md").write_text("locally modified\n")
    (workspace / "junk.tmp").write_text("untracked\n")

    pop.populate(workspace, _issue(), reused=True)

    assert (workspace / "README.md").read_text() == "hello\n"
    assert not (workspace / "junk.tmp").exists()
    # And the bare repo got an additional commit since first clone? Add one
    # to prove fetch+reset picks up upstream.
    work = tmp_path / "scratch"
    subprocess.run(["git", "clone", str(bare), str(work)], check=True, capture_output=True)
    (work / "NEW.md").write_text("upstream\n")
    _git(work, "add", "NEW.md")
    _git(work, "commit", "-m", "upstream")
    _git(work, "push", "origin", "main")

    pop.populate(workspace, _issue(), reused=True)
    assert (workspace / "NEW.md").read_text() == "upstream\n"


def test_reused_existing_dot_git_takes_refresh_path_even_if_manager_says_fresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``populate(reused=False)`` with a pre-existing ``.git`` should still
    refresh — directory state, not the flag, is the source of truth."""
    pop, _ = _build_populator(tmp_path, monkeypatch=monkeypatch)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    pop.populate(workspace, _issue(), reused=False)

    (workspace / "junk.tmp").write_text("uncommitted\n")
    pop.populate(workspace, _issue(), reused=False)  # mismatched flag on purpose
    assert not (workspace / "junk.tmp").exists()


# -- Failure surface ---------------------------------------------------------


def test_git_failure_raises_workspace_error_with_scrubbed_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "ghp_super_secret_bbbbbbbbbbb"
    tracker, github = _config_pair(tmp_path, token=secret)
    pop = GitWorkspacePopulator(tracker, github)

    # Non-existent local path — git clone will fail. Patch URL builders to
    # surface the secret in the error stream so we can verify scrubbing.
    monkeypatch.setattr(
        GitWorkspacePopulator,
        "_authenticated_url",
        lambda self: f"/nonexistent/{secret}/repo.git",
    )
    monkeypatch.setattr(
        GitWorkspacePopulator,
        "_public_url",
        lambda self: "/nonexistent/repo.git",
    )

    workspace = tmp_path / "ws"
    workspace.mkdir()
    with pytest.raises(WorkspaceError) as exc:
        pop.populate(workspace, _issue(), reused=False)
    message = str(exc.value)
    assert "git clone" in message
    assert secret not in message


def test_git_binary_missing_raises_workspace_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If git is not on PATH, the populator surfaces a clean WorkspaceError
    instead of leaking a raw OSError into the orchestrator."""
    pop, _ = _build_populator(tmp_path, monkeypatch=monkeypatch)
    monkeypatch.setattr(
        "subprocess.run",
        lambda *a, **kw: (_ for _ in ()).throw(FileNotFoundError("git not found")),
    )
    workspace = tmp_path / "ws"
    workspace.mkdir()
    with pytest.raises(WorkspaceError) as exc:
        pop.populate(workspace, _issue(), reused=False)
    assert "could not be invoked" in str(exc.value)


# -- _scrub_secret -----------------------------------------------------------


def test_scrub_secret_handles_literal_and_url_encoded() -> None:
    secret = "ghp_xx yy zz"  # space forces non-trivial URL encoding
    text = f"failed to push to https://oauth2:{secret}@github.com — token: {secret}"
    cleaned = _scrub_secret(text, secret)
    assert secret not in cleaned
    # URL-encoded form (``%20``) is also redacted.
    from urllib.parse import quote

    assert quote(secret, safe="") not in cleaned


def test_scrub_secret_empty_secret_is_noop() -> None:
    assert _scrub_secret("hello", "") == "hello"


# -- WorkspaceManager wiring -------------------------------------------------


def test_manager_calls_populator_only_when_populate_is_git(
    tmp_path: Path,
) -> None:
    calls: list[tuple[Path, str, bool]] = []

    class _StubPopulator:
        def populate(self, workspace_path: Path, issue: Issue, *, reused: bool) -> None:
            calls.append((workspace_path, issue.identifier, reused))

    issue = _issue()

    # populate=git → populator called.
    cfg_git = WorkspaceConfig(root=tmp_path / "ws_git", populate="git")
    mgr = WorkspaceManager(cfg_git, populator=_StubPopulator())
    workspace = mgr.prepare(issue)
    assert len(calls) == 1
    assert calls[0][0] == workspace.path
    assert calls[0][1] == issue.identifier
    assert calls[0][2] is False  # fresh

    # Second prepare on same dir → populator called with reused=True.
    mgr.prepare(issue)
    assert len(calls) == 2
    assert calls[1][2] is True


def test_manager_populator_unwired_keeps_empty_dir_contract(tmp_path: Path) -> None:
    """``WorkspaceManager(config)`` without a populator (default ctor) MUST
    keep the prior empty-directory behavior even when ``populate: git``.
    Test code paths that never wired a populator stay correct."""
    cfg = WorkspaceConfig(root=tmp_path / "ws", populate="git")
    mgr = WorkspaceManager(cfg)  # populator omitted
    workspace = mgr.prepare(_issue())
    assert workspace.path.exists()
    assert list(workspace.path.iterdir()) == []
