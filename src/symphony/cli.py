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
import os
import sys
from collections.abc import Sequence
from pathlib import Path

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
    run.add_argument(
        "--dashboard",
        action="store_true",
        help=(
            "Start a localhost dashboard server at http://127.0.0.1:8080. "
            "Serves live status as HTML (/) and JSON (/status.json). "
            "Read-only, no write controls."
        ),
    )
    run.add_argument(
        "--dashboard-port",
        type=int,
        default=8080,
        metavar="PORT",
        help="Dashboard server port (default: 8080). Only used with --dashboard.",
    )
    run.set_defaults(func=_cmd_run)

    init = subparsers.add_parser(
        "init",
        help="Generate a starter Symphony workflow.",
        description="Generate a starter Symphony workflow for a supported use case.",
    )
    init.add_argument(
        "template",
        choices=["github-implementer", "github-human-review", "github-production-line"],
        help="Workflow template to generate.",
    )
    init.add_argument(
        "--repo",
        required=True,
        metavar="OWNER/REPO",
        help="GitHub repository the workflow should operate on.",
    )
    init.add_argument(
        "--output",
        default="WORKFLOW.md",
        metavar="PATH",
        help="Workflow file to write (default: WORKFLOW.md).",
    )
    init.add_argument(
        "--force",
        action="store_true",
        help="Overwrite the output file if it already exists.",
    )
    init.add_argument(
        "--model",
        default=None,
        help=(
            "Model for the generated workflow. Defaults to claude-opus-4-7 "
            "for claude_code and gpt-5.3-codex for codex."
        ),
    )
    init.add_argument(
        "--provider",
        default="claude_code",
        choices=["claude_code", "codex"],
        help="Agent provider for generated agent roles (default: claude_code).",
    )
    init.add_argument(
        "--permission-mode",
        default="bypassPermissions",
        choices=["default", "acceptEdits", "bypassPermissions"],
        help="Provider permission mode for the generated workflow.",
    )
    init.add_argument(
        "--security-profile",
        default="trusted_unattended",
        choices=["restricted", "conservative", "trusted_unattended"],
        help="Security profile for the generated workflow.",
    )
    init.add_argument(
        "--create-labels",
        action="store_true",
        help="Create or update the standard Symphony labels in the target repo.",
    )
    init.add_argument(
        "--token-env",
        default="GITHUB_TOKEN",
        metavar="NAME",
        help="Environment variable holding the GitHub token (default: GITHUB_TOKEN).",
    )
    init.set_defaults(func=_cmd_init)

    return parser


def _cmd_init(args: argparse.Namespace) -> int:
    try:
        owner, repo = _parse_repo(args.repo)
    except ValueError as exc:
        print(f"symphony: init failed: {exc}", file=sys.stderr)
        return 1

    output = Path(args.output)
    if output.exists() and not args.force:
        print(
            f"symphony: init failed: {output} already exists; use --force to overwrite",
            file=sys.stderr,
        )
        return 1

    if args.template == "github-implementer":
        model = args.model or _default_model_for_provider(args.provider)
        workflow = _github_implementer_workflow(
            owner=owner,
            repo=repo,
            provider=args.provider,
            model=model,
            permission_mode=args.permission_mode,
            security_profile=args.security_profile,
            token_env=args.token_env,
        )
        ready_label = STANDARD_LABELS["symphony-ready"]["name"]
    else:
        model = args.model or _default_model_for_provider(args.provider)
        workflow = _github_role_workflow(
            owner=owner,
            repo=repo,
            provider=args.provider,
            model=model,
            permission_mode=args.permission_mode,
            security_profile=args.security_profile,
            token_env=args.token_env,
            production_line=args.template == "github-production-line",
        )
        ready_label = STANDARD_LABELS["symphony-ready-impl"]["name"]
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(workflow, encoding="utf-8")

    print(f"wrote {output}")
    print(f"repo: {owner}/{repo}")
    print(f"ready label: {ready_label}")
    if args.create_labels:
        token = os.environ.get(args.token_env, "")
        if not token:
            print(
                f"symphony: init failed: ${args.token_env} is not set for --create-labels",
                file=sys.stderr,
            )
            return 1
        _ensure_standard_labels(owner=owner, repo=repo, token=token)
        print("labels: created or updated")
    else:
        print("labels: skipped; pass --create-labels to create/update standard labels")
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    """Wire workflow → tracker → provider → workspace → orchestrator.

    Live subcommand: actually starts the orchestrator. Returns 0 on
    clean exit, 1 on workflow/config errors, raises non-zero exit on
    KeyboardInterrupt.
    """
    # Local imports keep the top-level CLI fast; heavy deps (httpx,
    # claude-agent-sdk) are only paid for when ``run`` is invoked.
    from symphony.config import ConfigError
    from symphony.dashboard_server import DashboardServer
    from symphony.github import GitHubTracker
    from symphony.orchestrator import Orchestrator
    from symphony.provider import ClaudeCodeProvider, CodexProvider
    from symphony.remote.dispatcher import build_ssh_remote_issue_dispatcher
    from symphony.workflow import WorkflowError, load_workflow
    from symphony.workflow_reload import WorkflowReloader
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
    if config.agent.provider == "claude_code":
        provider = ClaudeCodeProvider(tool_registry=_build_tool_registry(config, tracker))
    elif config.agent.provider == "codex":
        provider = CodexProvider(
            extra_env={
                "GITHUB_TOKEN": config.tracker.token,
                "GH_TOKEN": config.tracker.token,
            }
        )
    else:  # Defensive; config validation rejects this before runtime.
        raise AssertionError(f"unsupported provider {config.agent.provider!r}")
    workspace_mgr = WorkspaceManager(
        config.workspace,
        populator=_build_workspace_populator(config),
    )
    orchestrator = Orchestrator(
        config,
        tracker=tracker,
        provider=provider,
        workspace_manager=workspace_mgr,
        workflow_reloader=WorkflowReloader.from_workflow(workflow),
        remote_dispatcher=build_ssh_remote_issue_dispatcher(config),
    )

    # Start dashboard server if requested
    dashboard_server = None
    if args.dashboard:
        dashboard_server = DashboardServer(
            status_provider=orchestrator.status_snapshot,
            port=args.dashboard_port,
        )
        dashboard_server.start()

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
        if dashboard_server:
            dashboard_server.stop()
        try:
            tracker.close()
        except Exception as exc:  # noqa: BLE001 - cleanup must not mask outcome
            log.warning("tracker close failed: %s", exc)
    return 0


STANDARD_LABELS: dict[str, dict[str, str]] = {
    "symphony-ready": {
        "name": "symphony-ready",
        "color": "0e8a16",
        "description": "Eligible for Symphony to pick up",
    },
    "symphony-running": {
        "name": "symphony-running",
        "color": "fbca04",
        "description": "Symphony has claimed and is working on this",
    },
    "symphony-blocked": {
        "name": "symphony-blocked",
        "color": "d73a4a",
        "description": "Symphony hit a non-retryable or operator-required failure",
    },
    "symphony-done": {
        "name": "symphony-done",
        "color": "5319e7",
        "description": "Symphony completed work; PR opened or no-PR declared",
    },
    "symphony-ready-impl": {
        "name": "symphony-ready-impl",
        "color": "0e8a16",
        "description": "Ready for Symphony implementer role",
    },
    "symphony-implementing": {
        "name": "symphony-implementing",
        "color": "fbca04",
        "description": "Symphony implementer role is working",
    },
    "symphony-ready-review": {
        "name": "symphony-ready-review",
        "color": "1d76db",
        "description": "Ready for review role",
    },
    "symphony-reviewing": {
        "name": "symphony-reviewing",
        "color": "0052cc",
        "description": "Review role is working",
    },
    "symphony-changes-requested": {
        "name": "symphony-changes-requested",
        "color": "d93f0b",
        "description": "Review requested implementation changes",
    },
    "symphony-needs-design": {
        "name": "symphony-needs-design",
        "color": "5319e7",
        "description": "Needs design or leader decision before implementation",
    },
    "symphony-needs-leader": {
        "name": "symphony-needs-leader",
        "color": "5319e7",
        "description": "Needs leader decision before continuing",
    },
    "symphony-blocked-operator": {
        "name": "symphony-blocked-operator",
        "color": "d73a4a",
        "description": "Needs operator or leader intervention",
    },
    "symphony-leader-reviewing": {
        "name": "symphony-leader-reviewing",
        "color": "6f42c1",
        "description": "Leader role is working a gate",
    },
    "symphony-approved": {
        "name": "symphony-approved",
        "color": "0e8a16",
        "description": "Review approved",
    },
    "symphony-ready-verify": {
        "name": "symphony-ready-verify",
        "color": "1d76db",
        "description": "Ready for verifier role",
    },
    "symphony-verifying": {
        "name": "symphony-verifying",
        "color": "0052cc",
        "description": "Verifier role is working",
    },
    "symphony-ready-release": {
        "name": "symphony-ready-release",
        "color": "0e8a16",
        "description": "Ready for release role",
    },
    "symphony-releasing": {
        "name": "symphony-releasing",
        "color": "fbca04",
        "description": "Release role is working",
    },
    "symphony-released": {
        "name": "symphony-released",
        "color": "5319e7",
        "description": "Release role completed",
    },
}


def _parse_repo(value: str) -> tuple[str, str]:
    parts = value.strip().split("/")
    if len(parts) != 2 or not all(parts):
        raise ValueError("--repo must be in OWNER/REPO form")
    return parts[0], parts[1]


def _default_model_for_provider(provider: str) -> str:
    if provider == "codex":
        return "gpt-5.3-codex"
    return "claude-opus-4-7"


def _provider_runtime_section(
    *,
    provider: str,
    model: str,
    permission_mode: str,
) -> str:
    section = "codex" if provider == "codex" else "claude"
    return f"""{section}:
  model: {model}
  permission_mode: {permission_mode}
  session_store: .symphony/sessions
  transcript_store: .symphony/transcripts
  artifact_store: .symphony/runs
  turn_timeout_ms: 3600000
  stall_timeout_ms: 300000
  retry_resume_policy: resume_same_session"""


def _github_implementer_workflow(
    *,
    owner: str,
    repo: str,
    provider: str,
    model: str,
    permission_mode: str,
    security_profile: str,
    token_env: str,
) -> str:
    runtime_section = _provider_runtime_section(
        provider=provider,
        model=model,
        permission_mode=permission_mode,
    )
    return f"""---
tracker:
  kind: github
  owner: {owner}
  repo: {repo}
  token: ${token_env}
  include_labels: ["symphony-ready"]
  exclude_labels: ["symphony-running", "symphony-blocked", "symphony-done"]

agent:
  provider: {provider}
  max_concurrency: 1
  max_turns: 3

workspace:
  root: .symphony/workspaces
  populate: git

github:
  ready_label: symphony-ready
  claim_label: symphony-running
  blocked_label: symphony-blocked
  done_label: symphony-done
  branch_prefix: symphony
  base_branch: main
  draft_pr: true
  claim_comment: true
  pr_link_comment: true
  close_issue_on_done: false

security:
  profile: {security_profile}

{runtime_section}

polling:
  interval_ms: 60000

retry:
  max_attempts: 2
  initial_backoff_ms: 60000
  max_backoff_ms: 900000
  multiplier: 2.0
---
You are the Symphony implementer for {{{{ issue.identifier }}}}.

Repository: {owner}/{repo}
Issue title: {{{{ issue.title }}}}
Issue URL: {{{{ issue.url }}}}

Issue body:
{{{{ issue.body }}}}

Rules:
- Work on exactly this issue, one issue at a time.
- First inspect the issue, current labels, existing comments, and open PRs.
- If another contributor has claimed it, or an open PR already resolves it,
  do not duplicate the work; reply with
  `Symphony-No-PR: <reason>`.
- If the issue needs design approval before implementation, comment a concrete
  design proposal on the GitHub issue and reply with
  `Symphony-No-PR: design proposed`.
- Do not ask the local operator for clarification. Use GitHub issue comments
  for clarification/design questions.
- Create or update branch
  `symphony/{{{{ issue.owner }}}}-{{{{ issue.repo }}}}-{{{{ issue.number }}}}`
  when code changes are needed.
- Open or update a pull request against `main`.
- Include `Closes {{{{ issue.identifier }}}}` in the PR body.
- If a PR already exists for this issue from the Symphony branch, update that
  PR instead of opening a duplicate.
- Run the relevant tests/checks for the changed area when feasible.
- In the PR body, include a concise summary and the tests/checks you ran.
- Respond to review comments by updating the same PR.
- Do not add Linear assumptions.
- Do not finish as successful unless you opened/updated a PR or explicitly
  reply with `Symphony-No-PR: <reason>`.
"""


def _github_role_workflow(
    *,
    owner: str,
    repo: str,
    provider: str,
    model: str,
    permission_mode: str,
    security_profile: str,
    token_env: str,
    production_line: bool,
) -> str:
    runtime_section = _provider_runtime_section(
        provider=provider,
        model=model,
        permission_mode=permission_mode,
    )
    production_roles = ""
    production_states = ""
    production_transitions = ""
    reviewer_approved_to = "approved"
    reviewer_actor = "human"
    if production_line:
        reviewer_approved_to = "ready_verify"
        reviewer_actor = "agent"
        production_roles = """
  verifier:
    actor: human
    can_claim: [ready_verify]
    claim_state: verifying
    transitions:
      verified:
        from: verifying
        to: ready_release
        requires: review_comment
      verification_failed:
        from: verifying
        to: changes_requested
        requires: review_comment

  release:
    actor: human
    can_claim: [ready_release]
    claim_state: releasing
    transitions:
      released:
        from: releasing
        to: done
        requires: decision_comment
      release_blocked:
        from: releasing
        to: needs_leader
        requires: decision_comment
"""
        production_states = """
  ready_verify:
    labels: [symphony-ready-verify]
  verifying:
    labels: [symphony-verifying]
  ready_release:
    labels: [symphony-ready-release]
  releasing:
    labels: [symphony-releasing]
"""
        production_transitions = ""

    return f"""---
tracker:
  kind: github
  owner: {owner}
  repo: {repo}
  token: ${token_env}
  include_labels: []
  exclude_labels: [symphony-done]

agent:
  provider: {provider}
  max_concurrency: 1
  max_turns: 3

workspace:
  root: .symphony/workspaces
  populate: git

github:
  ready_label: symphony-ready-impl
  claim_label: symphony-implementing
  blocked_label: symphony-blocked-operator
  done_label: symphony-done
  branch_prefix: symphony
  base_branch: main
  draft_pr: true
  claim_comment: true
  pr_link_comment: true
  close_issue_on_done: false

roles:
  implementer:
    actor: agent
    provider: {provider}
    can_claim: [ready_impl, changes_requested]
    claim_state: implementing
    transitions:
      pr_delivered:
        from: implementing
        to: ready_review
        requires: pr_link
      design_needed:
        from: implementing
        to: needs_design
        requires: issue_comment
      operator_blocked:
        from: implementing
        to: blocked_operator
        requires: issue_comment
      no_work_needed:
        from: implementing
        to: done
        requires: issue_comment

  reviewer:
    actor: {reviewer_actor}
    provider: {provider}
    can_claim: [ready_review]
    claim_state: reviewing
    transitions:
      approved:
        from: reviewing
        to: {reviewer_approved_to}
        requires: pr_approval
      changes_requested:
        from: reviewing
        to: changes_requested
        requires: review_comment
      needs_leader:
        from: reviewing
        to: needs_leader
        requires: issue_comment

  leader:
    actor: hybrid
    provider: {provider}
    can_claim: [needs_design, needs_leader, blocked_operator]
    claim_state: leader_reviewing
    transitions:
      decision_to_impl:
        from: [leader_reviewing]
        to: ready_impl
        requires: decision_comment
{production_roles}
states:
  ready_impl:
    labels: [symphony-ready-impl]
  implementing:
    labels: [symphony-implementing]
  ready_review:
    labels: [symphony-ready-review]
  reviewing:
    labels: [symphony-reviewing]
  changes_requested:
    labels: [symphony-changes-requested]
  needs_design:
    labels: [symphony-needs-design]
    gate_owner: leader
  needs_leader:
    labels: [symphony-needs-leader]
    gate_owner: leader
  blocked_operator:
    labels: [symphony-blocked-operator]
    gate_owner: leader
  leader_reviewing:
    labels: [symphony-leader-reviewing]
  approved:
    labels: [symphony-approved]
{production_states}
  done:
    labels: [symphony-done]
    terminal: true
{production_transitions}
security:
  profile: {security_profile}

{runtime_section}

polling:
  interval_ms: 60000

retry:
  max_attempts: 2
  initial_backoff_ms: 60000
  max_backoff_ms: 900000
  multiplier: 2.0
---
You are working under the Symphony role contract injected above this prompt.

Repository: {owner}/{repo}
Issue title: {{{{ issue.title }}}}
Issue URL: {{{{ issue.url }}}}

Issue body:
{{{{ issue.body }}}}

Rules:
- Work on exactly this issue and exactly the active Symphony role.
- Inspect the issue, labels, comments, linked PRs, and existing branch before acting.
- If another contributor already owns the work or an open PR resolves it, reply with
  `Symphony-No-PR: <reason>`.
- If implementation needs design approval, comment a concrete design proposal on the
  GitHub issue and reply with `Symphony-No-PR: design proposed`.
- For code changes, create or update
  `symphony/{{{{ issue.owner }}}}-{{{{ issue.repo }}}}-{{{{ issue.number }}}}`.
- Open or update one pull request against `main` and include
  `Closes {{{{ issue.identifier }}}}` in the PR body.
- Use GitHub issue or PR comments for review responses, clarification, and audit trails.
- Run relevant tests/checks when feasible and summarize them in the PR.
- Do not edit Symphony state labels yourself; Symphony applies role-state
  transitions after validating your evidence.
- For role transitions, finish with
  `Symphony-Role-Outcome: <allowed_transition_name>` after producing the
  required GitHub-visible evidence.
- Do not add Linear assumptions.
- Do not finish as successful unless you produced the evidence required by the
  role contract or explicitly reply with `Symphony-No-PR: <reason>`.
"""


def _ensure_standard_labels(*, owner: str, repo: str, token: str) -> None:
    from symphony.github.client import GitHubClaimConflict, GitHubClient

    with GitHubClient(token) as client:
        for label in STANDARD_LABELS.values():
            path = f"/repos/{owner}/{repo}/labels"
            try:
                client.post(path, json_body=label)
            except GitHubClaimConflict:
                client.patch(f"{path}/{label['name']}", json_body=label)


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
