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
from symphony.cli import NotYetImplementedError, build_parser, main


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
