"""CLI tests covering both the argparse shape and the wired-up `run` command.

The wired-up tests exercise failure paths (missing workflow, invalid
config) without requiring real GitHub/Claude credentials. The success
path is exercised via the M3 E2E runbook, not unit tests.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from symphony import __version__
from symphony.cli import STANDARD_LABELS, NotYetImplementedError, build_parser, main
from symphony.models import Issue
from symphony.workflow import load_workflow, render_prompt


def test_version_flag(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["--version"])
    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    assert __version__ in out


def test_help_lists_run_command(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["--help"])
    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    assert "run" in out
    assert "init" in out
    assert "symphony" in out


def test_no_command_prints_help_and_returns_2(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main([])
    assert rc == 2
    err = capsys.readouterr().err
    assert "usage" in err.lower()


def test_run_requires_workflow_flag(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["run"])
    # argparse exits 2 on usage errors.
    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "--workflow" in err


def test_run_help_includes_once_and_log_level_flags(capsys: pytest.CaptureFixture[str]) -> None:
    """`--once` and `--log-level` are part of the M3 runbook contract."""
    with pytest.raises(SystemExit) as excinfo:
        main(["run", "--help"])
    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    assert "--once" in out
    assert "--log-level" in out


def test_init_github_implementer_writes_loadable_workflow(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test_token_value_1234567890")
    target = tmp_path / "WORKFLOW.md"

    rc = main(["init", "github-implementer", "--repo", "acme/proj", "--output", str(target)])

    assert rc == 0
    out = capsys.readouterr().out
    assert "wrote" in out
    text = target.read_text(encoding="utf-8")
    assert "symphony-ready" in text
    assert 'exclude_labels: ["symphony-running", "symphony-blocked", "symphony-done"]' in text
    assert "workspace:\n  root: .symphony/workspaces\n  populate: git" in text
    assert "model: claude-opus-4-7" in text
    workflow = load_workflow(target)
    assert workflow.config.tracker.owner == "acme"
    assert workflow.config.tracker.repo == "proj"
    assert workflow.config.workspace.populate == "git"
    prompt = render_prompt(
        workflow,
        issue=Issue(
            id="I_42",
            number=42,
            identifier="acme/proj#42",
            owner="acme",
            repo="proj",
            title="Fix bug",
            body="The detailed issue body matters.",
            state="open",
            url="https://github.com/acme/proj/issues/42",
        ),
    )
    assert "The detailed issue body matters." in prompt
    assert "First inspect the issue, current labels, existing comments, and open PRs" in prompt
    assert "Do not ask the local operator for clarification" in prompt
    assert "Use GitHub issue comments" in prompt
    assert "for clarification/design questions" in prompt
    assert "symphony/acme-proj-42" in prompt
    assert "Closes acme/proj#42" in prompt
    assert "update that\n  PR instead of opening a duplicate" in prompt
    assert "opened/updated a PR or explicitly" in prompt


def test_init_github_implementer_can_generate_codex_workflow(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test_token_value_1234567890")
    target = tmp_path / "WORKFLOW.md"

    rc = main(
        [
            "init",
            "github-implementer",
            "--provider",
            "codex",
            "--repo",
            "acme/proj",
            "--output",
            str(target),
        ]
    )

    assert rc == 0
    text = target.read_text(encoding="utf-8")
    assert "provider: codex" in text
    assert "codex:\n  model: gpt-5.3-codex" in text
    assert "claude:" not in text
    assert "Do not add Linear assumptions." in text
    workflow = load_workflow(target)
    assert workflow.config.agent.provider == "codex"
    assert workflow.config.claude.model == "gpt-5.3-codex"


def test_init_github_human_review_writes_role_workflow(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test_token_value_1234567890")
    target = tmp_path / "WORKFLOW.md"

    rc = main(["init", "github-human-review", "--repo", "acme/proj", "--output", str(target)])

    assert rc == 0
    out = capsys.readouterr().out
    assert "ready label: symphony-ready-impl" in out
    workflow = load_workflow(target)
    graph = workflow.config.role_graph
    assert graph is not None
    assert workflow.config.tracker.include_labels == ()
    assert graph.roles["implementer"].actor == "agent"
    assert graph.roles["reviewer"].actor == "human"
    assert graph.roles["leader"].actor == "hybrid"
    assert graph.transitions["pr_delivered"].to_state == "ready_review"
    assert graph.transitions["approved"].to_state == "approved"
    assert "symphony-ready-impl" in target.read_text(encoding="utf-8")


def test_init_github_human_review_can_generate_codex_role_workflow(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test_token_value_1234567890")
    target = tmp_path / "WORKFLOW.md"

    rc = main(
        [
            "init",
            "github-human-review",
            "--provider",
            "codex",
            "--repo",
            "acme/proj",
            "--output",
            str(target),
        ]
    )

    assert rc == 0
    workflow = load_workflow(target)
    graph = workflow.config.role_graph
    assert graph is not None
    assert workflow.config.agent.provider == "codex"
    assert graph.roles["implementer"].provider == "codex"
    assert graph.roles["leader"].provider == "codex"
    assert workflow.config.claude.model == "gpt-5.3-codex"


def test_init_github_production_line_writes_extended_role_workflow(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test_token_value_1234567890")
    target = tmp_path / "WORKFLOW.md"

    rc = main(
        [
            "init",
            "github-production-line",
            "--repo",
            "acme/proj",
            "--output",
            str(target),
        ]
    )

    assert rc == 0
    workflow = load_workflow(target)
    graph = workflow.config.role_graph
    assert graph is not None
    assert graph.roles["reviewer"].actor == "agent"
    assert graph.roles["reviewer"].provider == "claude_code"
    assert graph.roles["verifier"].actor == "human"
    assert graph.roles["release"].actor == "human"
    assert graph.transitions["approved"].to_state == "ready_verify"
    assert graph.transitions["verified"].to_state == "ready_release"
    assert graph.transitions["released"].to_state == "done"
    assert graph.states["done"].terminal is True


def test_standard_labels_include_role_states() -> None:
    for name in (
        "symphony-ready-impl",
        "symphony-implementing",
        "symphony-ready-review",
        "symphony-needs-design",
        "symphony-blocked-operator",
        "symphony-ready-verify",
        "symphony-ready-release",
    ):
        assert STANDARD_LABELS[name]["name"] == name


def test_init_refuses_to_overwrite_without_force(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    target = tmp_path / "WORKFLOW.md"
    target.write_text("existing", encoding="utf-8")

    rc = main(["init", "github-implementer", "--repo", "acme/proj", "--output", str(target)])

    assert rc == 1
    assert target.read_text(encoding="utf-8") == "existing"
    assert "already exists" in capsys.readouterr().err


def test_init_rejects_invalid_repo(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    rc = main(
        [
            "init",
            "github-implementer",
            "--repo",
            "not-a-repo",
            "--output",
            str(tmp_path / "WORKFLOW.md"),
        ]
    )

    assert rc == 1
    assert "OWNER/REPO" in capsys.readouterr().err


def test_run_missing_workflow_file_exits_1(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    rc = main(["run", "--workflow", str(tmp_path / "missing.md")])
    assert rc == 1
    err = capsys.readouterr().err
    assert "workflow load failed" in err.lower()


def test_run_invalid_yaml_exits_1(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    bad = tmp_path / "bad.md"
    bad.write_text("---\nagent: [unterminated\n---\nbody\n", encoding="utf-8")
    rc = main(["run", "--workflow", str(bad)])
    assert rc == 1


def test_run_subcommand_exits_one_via_subprocess_for_missing_workflow(tmp_path: Path) -> None:
    """End-to-end subprocess invocation for the failure path. Catches
    drift in the wired command's exit semantics that an in-process
    import test can't (e.g. import-time errors)."""
    result = subprocess.run(
        [sys.executable, "-m", "symphony", "run", "--workflow", str(tmp_path / "nope.md")],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "workflow load failed" in result.stderr.lower()


def test_unknown_command_is_rejected(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["bogus"])
    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "invalid choice" in err.lower() or "bogus" in err


def test_parser_object_is_constructible() -> None:
    parser = build_parser()
    assert parser.prog == "symphony"


def test_not_yet_implemented_class_still_exported() -> None:
    """`NotYetImplementedError` is no longer raised by `run` (which is
    now wired up), but the class stays exported so future subcommands
    can use the same convention. Sanity-check the constants don't drift.
    """
    assert NotYetImplementedError.EXIT_CODE == 1
    err = NotYetImplementedError("future subcommand")
    assert isinstance(err.code, str)
    assert err.code.startswith("symphony: not yet implemented:")
