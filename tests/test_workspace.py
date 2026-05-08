"""Tests for the workspace manager.

Covers SPEC.md §8 (Workspace Contract) and the acceptance criteria on
issue #6.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from symphony.config import WorkspaceConfig
from symphony.models import Issue, Workspace
from symphony.workspace import (
    HookResult,
    WorkspaceError,
    WorkspaceManager,
    _sanitize_component,
)


def _issue(
    *,
    owner: str = "jimoosciuc",
    repo: str = "symphony-cc",
    number: int = 42,
) -> Issue:
    return Issue(
        id="I_kw1",
        number=number,
        identifier=f"{owner}/{repo}#{number}",
        owner=owner,
        repo=repo,
        title="Title",
        body="Body",
        state="open",
        url=f"https://github.com/{owner}/{repo}/issues/{number}",
    )


@pytest.fixture
def config(tmp_path: Path) -> WorkspaceConfig:
    return WorkspaceConfig(root=tmp_path / "ws")


# -- Naming -------------------------------------------------------------------


def test_workspace_path_uses_owner_repo_number(config: WorkspaceConfig) -> None:
    mgr = WorkspaceManager(config)
    path = mgr.workspace_path(_issue(owner="acme", repo="proj", number=7))
    assert path.name == "acme_proj_7"
    assert path.parent == mgr.root


def test_workspace_path_sanitizes_disallowed_characters(config: WorkspaceConfig) -> None:
    mgr = WorkspaceManager(config)
    path = mgr.workspace_path(_issue(owner="acme/inc", repo="my proj!", number=3))
    # `/` and ` ` and `!` all become `_`.
    assert path.name == "acme_inc_my_proj__3"


def test_workspace_path_preserves_dot_dash_underscore(config: WorkspaceConfig) -> None:
    mgr = WorkspaceManager(config)
    path = mgr.workspace_path(_issue(owner="org.team", repo="repo-1_alt", number=2))
    assert path.name == "org.team_repo-1_alt_2"


def test_issue_number_must_be_positive_int(config: WorkspaceConfig) -> None:
    mgr = WorkspaceManager(config)
    with pytest.raises(WorkspaceError) as excinfo:
        mgr.workspace_path(_issue(number=0))
    assert excinfo.value.location == "issue.number"


# -- Path traversal / containment --------------------------------------------


def test_dotdot_owner_rejected_pre_path_build(config: WorkspaceConfig) -> None:
    """`..` survives the character-class sanitizer (`.` is allowed) but is
    rejected by the dedicated reserved-name guard."""
    mgr = WorkspaceManager(config)
    with pytest.raises(WorkspaceError) as excinfo:
        mgr.workspace_path(_issue(owner="..", repo="r", number=1))
    assert excinfo.value.location == "issue.owner"


def test_dot_owner_rejected(config: WorkspaceConfig) -> None:
    mgr = WorkspaceManager(config)
    with pytest.raises(WorkspaceError):
        mgr.workspace_path(_issue(owner=".", repo="r", number=1))


def test_all_dots_owner_rejected(config: WorkspaceConfig) -> None:
    mgr = WorkspaceManager(config)
    with pytest.raises(WorkspaceError):
        mgr.workspace_path(_issue(owner="....", repo="r", number=1))


def test_empty_repo_rejected(config: WorkspaceConfig) -> None:
    mgr = WorkspaceManager(config)
    with pytest.raises(WorkspaceError):
        mgr.workspace_path(_issue(owner="o", repo="", number=1))


def test_sanitizer_replaces_path_separators(config: WorkspaceConfig) -> None:
    """Even with backslashes (Windows-style) we must not produce a multi-segment
    path. The character-class sanitizer turns them into underscores."""
    out = _sanitize_component("a\\b/c", location="x")
    assert "/" not in out
    assert "\\" not in out
    assert out == "a_b_c"


# -- New / reuse --------------------------------------------------------------


def test_prepare_creates_new_workspace(config: WorkspaceConfig) -> None:
    mgr = WorkspaceManager(config)
    issue = _issue()
    ws = mgr.prepare(issue)
    assert isinstance(ws, Workspace)
    assert ws.reused is False
    assert ws.path.is_dir()
    assert ws.workspace_key == ws.path.name
    assert ws.repo_path == ws.path
    assert ws.issue_identifier == issue.identifier


def test_prepare_reuses_existing_workspace(config: WorkspaceConfig) -> None:
    mgr = WorkspaceManager(config)
    issue = _issue()
    first = mgr.prepare(issue)
    # Drop a marker file so we can prove the directory is preserved.
    marker = first.path / "marker.txt"
    marker.write_text("hello", encoding="utf-8")
    second = mgr.prepare(issue)
    assert second.reused is True
    assert second.path == first.path
    assert marker.read_text(encoding="utf-8") == "hello"


def test_prepare_creates_root_on_demand(tmp_path: Path) -> None:
    nested_root = tmp_path / "deeper" / "nest" / "ws"
    mgr = WorkspaceManager(WorkspaceConfig(root=nested_root))
    ws = mgr.prepare(_issue())
    assert ws.path.is_dir()
    assert nested_root.is_dir()


def test_prepare_refuses_to_clobber_a_file(config: WorkspaceConfig) -> None:
    mgr = WorkspaceManager(config)
    config.root.mkdir(parents=True)
    issue = _issue()
    target = mgr.workspace_path(issue)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("oops", encoding="utf-8")  # file at the workspace path
    with pytest.raises(WorkspaceError) as excinfo:
        mgr.prepare(issue)
    assert "not a directory" in excinfo.value.message


# -- Hooks --------------------------------------------------------------------


@pytest.fixture
def mgr_with_hooks(tmp_path: Path) -> WorkspaceManager:
    cfg = WorkspaceConfig(
        root=tmp_path / "ws",
        after_create=f"{sys.executable} -c 'print(\"created\")'",
        before_run=f"{sys.executable} -c 'import sys; sys.exit(7)'",
        after_run=(
            # Print cwd so a test can assert hooks run with cwd set to
            # the workspace path.
            f"{sys.executable} -c 'import os; print(os.getcwd())'"
        ),
        before_delete=(
            # Sleep longer than the configured timeout so the test can
            # assert timeout behavior.
            f"{sys.executable} -c 'import time; time.sleep(5)'"
        ),
        hook_timeout_ms=1000,
    )
    return WorkspaceManager(cfg)


def test_hook_success_returns_zero_returncode(mgr_with_hooks: WorkspaceManager) -> None:
    ws = mgr_with_hooks.prepare(_issue())
    result = mgr_with_hooks.run_hook("after_create", ws)
    assert result is not None
    assert result.succeeded is True
    assert result.returncode == 0
    assert "created" in result.stdout
    assert result.timed_out is False


def test_hook_non_zero_exit_is_returned_not_raised(
    mgr_with_hooks: WorkspaceManager,
) -> None:
    ws = mgr_with_hooks.prepare(_issue())
    result = mgr_with_hooks.run_hook("before_run", ws)
    assert result is not None
    assert result.succeeded is False
    assert result.returncode == 7
    assert result.timed_out is False


def test_hook_runs_with_cwd_set_to_workspace(mgr_with_hooks: WorkspaceManager) -> None:
    ws = mgr_with_hooks.prepare(_issue())
    result = mgr_with_hooks.run_hook("after_run", ws)
    assert result is not None
    assert result.succeeded
    # Resolve both sides — macOS adds /private prefix to /tmp paths.
    assert Path(result.stdout.strip()).resolve() == ws.path.resolve()


def test_hook_timeout_is_reported(mgr_with_hooks: WorkspaceManager) -> None:
    ws = mgr_with_hooks.prepare(_issue())
    result = mgr_with_hooks.run_hook("before_delete", ws)
    assert result is not None
    assert result.timed_out is True
    assert result.succeeded is False


def test_before_delete_on_missing_workspace_raises(
    mgr_with_hooks: WorkspaceManager,
) -> None:
    """Lifecycle shell commands are workspace-scoped by contract. If the
    workspace path is missing — including for before_delete, which used to
    silently fall back to the Symphony process cwd — run_hook MUST raise
    WorkspaceError without executing the command. The orchestrator decides
    whether cleanup can continue.
    """
    ws = mgr_with_hooks.prepare(_issue())
    # Simulate the workspace having already been removed before
    # before_delete runs. shutil.rmtree to mimic real cleanup semantics.
    import shutil as _shutil

    _shutil.rmtree(ws.path)
    assert not ws.path.exists()
    with pytest.raises(WorkspaceError) as excinfo:
        mgr_with_hooks.run_hook("before_delete", ws)
    assert excinfo.value.location == "workspace.path"
    assert "does not exist" in excinfo.value.message
    assert "before_delete" in excinfo.value.message


def test_unconfigured_hook_returns_none(tmp_path: Path) -> None:
    mgr = WorkspaceManager(WorkspaceConfig(root=tmp_path / "ws"))
    ws = mgr.prepare(_issue())
    assert mgr.run_hook("after_create", ws) is None


def test_hook_result_dataclass_carries_command_name(
    mgr_with_hooks: WorkspaceManager,
) -> None:
    ws = mgr_with_hooks.prepare(_issue())
    result = mgr_with_hooks.run_hook("after_create", ws)
    assert isinstance(result, HookResult)
    assert result.name == "after_create"
    assert "print(" in result.command


# -- delete() -----------------------------------------------------------------


def test_delete_removes_workspace(config: WorkspaceConfig) -> None:
    mgr = WorkspaceManager(config)
    ws = mgr.prepare(_issue())
    (ws.path / "file").write_text("x", encoding="utf-8")
    mgr.delete(ws)
    assert not ws.path.exists()


def test_delete_refuses_root_itself(config: WorkspaceConfig, tmp_path: Path) -> None:
    mgr = WorkspaceManager(config)
    config.root.mkdir(parents=True)
    fake_ws = Workspace(
        issue_identifier="x",
        workspace_key="root",
        path=mgr.root,
        repo_path=mgr.root,
        created_at=__import__("datetime").datetime.now(),
        reused=True,
    )
    with pytest.raises(WorkspaceError):
        mgr.delete(fake_ws)


def test_delete_refuses_path_outside_root(config: WorkspaceConfig, tmp_path: Path) -> None:
    mgr = WorkspaceManager(config)
    rogue = tmp_path / "elsewhere"
    rogue.mkdir()
    fake_ws = Workspace(
        issue_identifier="x",
        workspace_key="elsewhere",
        path=rogue,
        repo_path=rogue,
        created_at=__import__("datetime").datetime.now(),
        reused=True,
    )
    with pytest.raises(WorkspaceError):
        mgr.delete(fake_ws)
    assert rogue.exists()  # still there


# -- workspace_path is deterministic ------------------------------------------


def test_workspace_path_is_pure_no_io(config: WorkspaceConfig) -> None:
    mgr = WorkspaceManager(config)
    issue = _issue()
    p1 = mgr.workspace_path(issue)
    p2 = mgr.workspace_path(issue)
    assert p1 == p2
    assert not p1.exists()  # workspace_path must not have done I/O
