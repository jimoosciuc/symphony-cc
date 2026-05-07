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

    Inherits ``SystemExit`` so argparse callers exit with a non-zero status
    instead of a Python traceback. The exit code is ``2`` to distinguish from
    argparse's own ``2`` (usage errors, which always print a usage line first).
    """

    def __init__(self, message: str) -> None:
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
