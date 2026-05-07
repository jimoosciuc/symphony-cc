"""Smoke tests for the symphony CLI surface.

These exercise the argument parser only — no runtime behavior is wired up yet.
They lock down the CLI shape so later issues that wire real behavior do not
accidentally rename the entry point or change required flags.
"""

from __future__ import annotations

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


def test_run_with_workflow_raises_not_yet_implemented() -> None:
    with pytest.raises(NotYetImplementedError) as excinfo:
        main(["run", "--workflow", "WORKFLOW.example.md"])
    msg = str(excinfo.value)
    assert "not yet implemented" in msg
    assert "WORKFLOW.example.md" in msg


def test_unknown_command_is_rejected(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["bogus"])
    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "invalid choice" in err.lower() or "bogus" in err


def test_parser_object_is_constructible() -> None:
    # Guards against import-time regressions — later issues will extend this.
    parser = build_parser()
    assert parser.prog == "symphony"
