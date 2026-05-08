"""Symphony command-line interface.

The ``symphony`` console script and ``python -m symphony`` both route
through :func:`main`. Today the only subcommand is ``run``; future
issues may add ``status``, ``replay``, etc.

``symphony run --workflow PATH`` loads the workflow file, instantiates
the GitHub tracker / Claude Code provider / workspace manager /
orchestrator, and drives one or more poll ticks.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
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
            "config, and starts the orchestrator."
        ),
    )
    run.add_argument(
        "--workflow",
        required=True,
        metavar="PATH",
        help="Path to the workflow file (e.g. WORKFLOW.md).",
    )
    run.add_argument(
        "--once",
        action="store_true",
        help=(
            "Run a single poll tick and exit. Useful for smoke tests, the "
            "M3 E2E run, and CI runbooks. Without this flag the daemon "
            "polls forever at `polling.interval_ms`."
        ),
    )
    run.add_argument(
        "--log-level",
        default="info",
        choices=["debug", "info", "warning", "error", "critical"],
        help="Override the workflow file's logging.level for this invocation.",
    )
    run.set_defaults(func=_cmd_run)

    return parser


def _cmd_run(args: argparse.Namespace) -> int:
    """Wire workflow → tracker → provider → workspace → orchestrator.

    Live subcommand: actually starts the orchestrator. Returns 0 on
    clean exit, 1 on workflow/config errors, raises non-zero exit on
    KeyboardInterrupt.
    """
    # Local imports keep the top-level CLI fast; heavy deps (httpx,
    # claude-agent-sdk) are only paid for when ``run`` is invoked.
    from symphony.config import ConfigError
    from symphony.github import GitHubTracker
    from symphony.orchestrator import Orchestrator
    from symphony.provider import ClaudeCodeProvider
    from symphony.workflow import WorkflowError, load_workflow
    from symphony.workspace import WorkspaceManager

    _setup_logging(args.log_level)
    log = logging.getLogger("symphony.cli")

    try:
        workflow = load_workflow(args.workflow)
    except (ConfigError, WorkflowError) as exc:
        print(f"symphony: workflow load failed: {exc}", file=sys.stderr)
        return 1

    config = workflow.config
    for warning in config.warnings:
        log.warning("config warning: %s: %s", warning.location, warning.message)

    log.info(
        "starting symphony run: workflow=%s tracker=%s/%s provider=%s",
        workflow.path,
        config.tracker.owner,
        config.tracker.repo,
        config.agent.provider,
    )

    tracker = GitHubTracker(config.tracker, config.github)
    provider = ClaudeCodeProvider(tool_registry=_build_tool_registry(config, tracker))
    workspace_mgr = WorkspaceManager(
        config.workspace,
        populator=_build_workspace_populator(config),
    )
    orchestrator = Orchestrator(
        config,
        tracker=tracker,
        provider=provider,
        workspace_manager=workspace_mgr,
    )

    try:
        if args.once:
            result = asyncio.run(_run_once_with_recovery(orchestrator))
            _print_tick_summary(result, recovery=orchestrator.recovery_decisions)
        else:
            asyncio.run(_run_forever_with_recovery(orchestrator))
    except KeyboardInterrupt:
        log.info("received interrupt, exiting")
        return 130  # standard SIGINT exit code
    finally:
        try:
            tracker.close()
        except Exception as exc:  # noqa: BLE001 - cleanup must not mask outcome
            log.warning("tracker close failed: %s", exc)
    return 0


def _setup_logging(level_name: str) -> None:
    level = getattr(logging, level_name.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )


def _print_tick_summary(result: object, *, recovery: object = None) -> None:
    """Pretty-print a TickResult to stdout for --once invocations.

    Imported lazily so the type isn't required for the help message.
    """
    if recovery:
        print("symphony recovery decisions:")
        for decision in recovery:  # type: ignore[union-attr]
            action = getattr(decision, "action", "?")
            ident = getattr(decision, "issue_identifier", "?")
            reason = getattr(decision, "reason", "")
            restored = getattr(decision, "restored_session_id", None)
            extra = f" (session={restored})" if restored else ""
            print(f"  {action} {ident}{extra}: {reason}")
    print("symphony tick result:")
    for field_name in (
        "dispatched",
        "finished",
        "reconciled_cancelled",
        "skipped_claim_conflict",
        "retries_scheduled",
    ):
        items = getattr(result, field_name, [])
        print(f"  {field_name}: {list(items)}")


async def _run_once_with_recovery(orchestrator: object) -> object:
    """Recover persisted records, then run one poll tick."""
    await orchestrator.recover()  # type: ignore[union-attr]
    return await orchestrator.run_once()  # type: ignore[union-attr]


async def _run_forever_with_recovery(orchestrator: object) -> None:
    """Recover persisted records once, then enter the long-running loop."""
    await orchestrator.recover()  # type: ignore[union-attr]
    await orchestrator.run_forever()  # type: ignore[union-attr]


def _build_tool_registry(config: object, tracker: object) -> object | None:
    """Construct a :class:`ToolRegistry` honoring ``agent.tools.*`` knobs.

    Returns ``None`` when no tools are enabled so the provider's
    construction stays a one-liner. Tools share the tracker's
    :class:`GitHubClient` (one auth context per run) — the raw token
    only ever lives inside the client; Claude never sees it.
    """
    tools_cfg = getattr(getattr(config, "agent", None), "tools", None)
    if tools_cfg is None:
        return None
    gql_cfg = getattr(tools_cfg, "github_graphql", None)
    if gql_cfg is None or not getattr(gql_cfg, "enabled", False):
        return None
    from symphony.provider.claude_code import ToolRegistry
    from symphony.tools.github_graphql import GitHubGraphQLTool

    registry = ToolRegistry()
    registry.register_github_graphql(GitHubGraphQLTool(tracker.client))  # type: ignore[attr-defined]
    return registry


def _build_workspace_populator(config: object) -> object | None:
    """Construct the workspace populator honoring ``workspace.populate``.

    Returns ``None`` when no real population strategy is configured so the
    :class:`WorkspaceManager` keeps its empty-directory contract for
    test wiring. Production picks up the git populator whenever
    ``workspace.populate: git`` is set, sourcing the token from
    ``tracker`` and the base branch from ``github``.
    """
    workspace_cfg = getattr(config, "workspace", None)
    populate = getattr(workspace_cfg, "populate", None)
    if populate != "git":
        return None
    from symphony.workspace import GitWorkspacePopulator

    return GitWorkspacePopulator(config.tracker, config.github)  # type: ignore[attr-defined]


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not getattr(args, "command", None):
        parser.print_help(sys.stderr)
        return 2

    return args.func(args)
