"""Symphony command-line interface.

The CLI surface is intentionally minimal in this milestone. Subcommands are
registered here, but each command's behavior is implemented by a later issue.
Until then, commands fail with a clear ``not yet implemented`` message so that
operators (and dependent issues) can see exactly which boundary is missing.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from symphony import __version__


class NotYetImplementedError(SystemExit):
    """Raised when a CLI subcommand has no runtime wired up yet.

    Inherits :class:`SystemExit` so the CLI exits cleanly without a Python
    traceback. When ``SystemExit.code`` is a non-int, the interpreter prints
    ``str(code)`` to stderr and exits with status ``1``; that is the behavior
    we want and rely on. The constant :attr:`EXIT_CODE` documents this so it
    is easy to find from grep, and is asserted by the CLI tests.

    Note: status ``1`` is intentionally distinct from argparse's ``2``, which
    argparse uses for usage errors (and which always prints a usage line
    first). A ``not yet implemented`` failure is a runtime gap, not a usage
    bug, so it gets the generic-failure code.
    """

    EXIT_CODE = 1

    def __init__(self, message: str) -> None:
        # SystemExit prints str(code) to stderr and exits with status 1
        # whenever code is not an int. Keeping the message-as-code form
        # gives us EXIT_CODE == 1 plus a human-readable stderr line for free.
        super().__init__(f"symphony: not yet implemented: {message}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="symphony",
        description="Claude Code first, GitHub first agent orchestrator.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"symphony {__version__}",
    )

    subparsers = parser.add_subparsers(dest="command", metavar="<command>")

    run = subparsers.add_parser(
        "run",
        help="Run the Symphony daemon against a workflow file.",
        description=(
            "Run the Symphony daemon. Loads the workflow file, validates "
            "config, and starts the orchestrator. Not yet implemented."
        ),
    )
    run.add_argument(
        "--workflow",
        required=True,
        metavar="PATH",
        help="Path to the workflow file (e.g. WORKFLOW.md).",
    )
    run.set_defaults(func=_cmd_run)

    return parser


def _cmd_run(args: argparse.Namespace) -> int:
    raise NotYetImplementedError(
        f"`symphony run --workflow {args.workflow}` will be wired up by later "
        "issues (workflow loader, workspace manager, orchestrator)."
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not getattr(args, "command", None):
        parser.print_help(sys.stderr)
        return 2

    return args.func(args)
