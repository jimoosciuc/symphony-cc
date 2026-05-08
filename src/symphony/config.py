"""Typed configuration model for ``WORKFLOW.md``.

The workflow loader (``symphony.workflow``) parses the YAML front matter
and hands the raw mapping to :func:`build_config`, which:

1. Validates that required top-level sections are present.
2. Applies defaults for optional fields per ``SPEC.md`` §7.
3. Resolves scalar ``$ENV_VAR`` references against ``os.environ``.
4. Normalizes relative filesystem paths against the workflow file's
   directory so that runs are deterministic regardless of cwd.
5. Returns a single immutable :class:`WorkflowConfig` tree.

Validation errors are raised as :class:`ConfigError` with a path-style
location (e.g. ``claude.permission_mode``) so that the CLI can present
operator-facing messages without a Python traceback.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from symphony.remote.config import RemoteConfig

# -- Errors --------------------------------------------------------------------


class ConfigError(ValueError):
    """Raised when ``WORKFLOW.md`` config fails validation.

    Carries a dotted ``location`` (e.g. ``"claude.permission_mode"``) so
    callers can show the operator exactly which field is wrong.
    """

    def __init__(self, location: str, message: str) -> None:
        super().__init__(f"{location}: {message}")
        self.location = location
        self.message = message


@dataclass(frozen=True, slots=True)
class ConfigWarning:
    """A non-fatal validation warning attached to a built :class:`WorkflowConfig`.

    Surfaced for settings that the operator may have chosen on purpose
    (e.g. ``claude.permission_mode = bypassPermissions``) but that
    Symphony wants to flag as unsafe / explicit-opt-in. The CLI is
    expected to print these once at startup.
    """

    location: str
    message: str


# -- Constants -----------------------------------------------------------------

REQUIRED_SECTIONS: tuple[str, ...] = (
    "tracker",
    "agent",
    "workspace",
    "claude",
    "github",
)

ALLOWED_PERMISSION_MODES: frozenset[str] = frozenset(
    # `plan` is intentionally NOT in this set: it blocks on human
    # confirmation, and Symphony has no human-in-the-loop for provider
    # turns (per docs/claude-provider.md §7). Reject it at config time
    # so the failure is clear at the workflow boundary, not at the first
    # turn. `bypassPermissions` is allowed but emits a warning — see
    # _build_claude.
    {"default", "acceptEdits", "bypassPermissions", "dontAsk", "auto"}
)

REJECTED_PERMISSION_MODES: frozenset[str] = frozenset({"plan"})

WARN_PERMISSION_MODES: frozenset[str] = frozenset({"bypassPermissions"})

ALLOWED_RETRY_RESUME_POLICIES: frozenset[str] = frozenset(
    {"resume_same_session", "new_session_with_summary", "fail_closed"}
)

ALLOWED_SECURITY_PROFILES: frozenset[str] = frozenset(
    {"conservative", "trusted_unattended", "restricted"}
)


# -- Section dataclasses -------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TrackerConfig:
    kind: str
    owner: str
    repo: str
    token: str
    include_labels: tuple[str, ...] = ()
    exclude_labels: tuple[str, ...] = ()
    terminal_labels: tuple[str, ...] = ()
    active_states: tuple[str, ...] = ("open",)


@dataclass(frozen=True, slots=True)
class GitHubGraphQLToolConfig:
    """Workflow knob for the optional ``github_graphql`` tool (SPEC §18).

    When ``enabled`` is true, the Claude provider exposes a ``github_graphql``
    MCP tool that runs a single GraphQL operation against Symphony's
    configured ``tracker.token`` (the raw token never enters the model
    context — see ``src/symphony/tools/github_graphql.py``).
    """

    enabled: bool = False


@dataclass(frozen=True, slots=True)
class AgentToolsConfig:
    """Optional client-side tools surfaced to the Claude session.

    Each tool is gated behind its own subsection so a workflow can enable
    one without auto-enabling the rest. SPEC §18 says ``MAY``, not
    ``MUST`` — defaults are off.
    """

    github_graphql: GitHubGraphQLToolConfig = field(default_factory=GitHubGraphQLToolConfig)


@dataclass(frozen=True, slots=True)
class AgentConfig:
    provider: str = "claude_code"
    max_concurrency: int = 1
    max_turns: int = 3
    tools: AgentToolsConfig = field(default_factory=AgentToolsConfig)


@dataclass(frozen=True, slots=True)
class WorkspaceCleanupConfig:
    """Workflow knob for workspace deletion (M5.6 #65 / parent #52).

    Schema-only ticket. The executor that consumes these flags lands in
    #66; this dataclass plus its validators in :func:`_build_workspace_cleanup`
    are the contract that #66 builds against.

    Defaults are **default-safe**: ``enabled=False`` preserves SPEC §8
    "preserved workspaces MUST be reused" semantics. Existing workflows
    without a ``workspace.cleanup`` section continue to behave identically.

    Trigger fields (``on_terminal_issue`` / ``on_closed_pr`` / ``max_age_days``)
    are inert when ``enabled=False`` — the executor (#66) MUST consult
    ``enabled`` first. When ``enabled=True``, at least one trigger MUST
    be set; the validator rejects an enabled-with-no-trigger combo
    because such a config would never delete anything (operator error).

    ``max_age_days=0`` is normalized to ``None`` / unset so operators can
    disable age-based cleanup without removing the key.
    """

    enabled: bool = False
    on_terminal_issue: bool = False
    on_closed_pr: bool = False
    max_age_days: int | None = None
    dry_run: bool = False


@dataclass(frozen=True, slots=True)
class WorkspaceConfig:
    root: Path
    populate: str = "git"
    remote: str = "origin"
    after_create: str | None = None
    before_run: str | None = None
    after_run: str | None = None
    before_delete: str | None = None
    hook_timeout_ms: int = 300_000
    cleanup: WorkspaceCleanupConfig = field(default_factory=WorkspaceCleanupConfig)


@dataclass(frozen=True, slots=True)
class GitHubProjectConfig:
    enabled: bool = False
    owner: str | None = None
    number: int | None = None
    status_field: str = "Status"
    ready_values: tuple[str, ...] = ("Ready",)
    running_value: str = "In Progress"
    review_value: str = "Review"


@dataclass(frozen=True, slots=True)
class GitHubConfig:
    claim_label: str = "symphony-running"
    ready_label: str = "symphony-ready"
    blocked_label: str = "symphony-blocked"
    done_label: str = "symphony-done"
    branch_prefix: str = "symphony"
    base_branch: str = "main"
    draft_pr: bool = True
    claim_comment: bool = True
    pr_link_comment: bool = True
    close_issue_on_done: bool = False
    project: GitHubProjectConfig = field(default_factory=GitHubProjectConfig)


@dataclass(frozen=True, slots=True)
class ArtifactRetentionConfig:
    """Workflow knob for artifact-store retention (M5.6 #65 / parent #52).

    Per the leader's "modeled separately from workspace cleanup"
    requirement on #65: artifacts are audit evidence and follow simpler
    retention rules. The only trigger is age — terminal-issue and
    closed-PR triggers are intentionally NOT supported here because an
    operator triaging a misleading-success run weeks later still needs
    the `events.jsonl` and `terminal.json` from that attempt.

    Defaults are **default-safe**: ``enabled=False`` preserves all
    artifacts. Existing workflows without a ``claude.artifact_retention``
    section continue to behave identically.

    When ``enabled=True``, ``max_age_days`` MUST be set; the validator
    rejects an enabled-with-no-trigger combo.
    """

    enabled: bool = False
    max_age_days: int | None = None
    dry_run: bool = False


@dataclass(frozen=True, slots=True)
class ClaudeConfig:
    model: str
    permission_mode: str
    session_store: Path
    transcript_store: Path
    artifact_store: Path
    turn_timeout_ms: int = 3_600_000
    read_timeout_ms: int = 30_000
    stall_timeout_ms: int = 300_000
    retry_resume_policy: str = "resume_same_session"
    artifact_retention: ArtifactRetentionConfig = field(
        default_factory=ArtifactRetentionConfig
    )


@dataclass(frozen=True, slots=True)
class PollingConfig:
    interval_ms: int = 60_000
    reconcile_interval_ms: int = 30_000


@dataclass(frozen=True, slots=True)
class RetryConfig:
    max_attempts: int = 3
    initial_backoff_ms: int = 60_000
    max_backoff_ms: int = 900_000
    multiplier: float = 2.0


@dataclass(frozen=True, slots=True)
class LoggingConfig:
    level: str = "info"
    jsonl_path: Path | None = None
    redact_keys: tuple[str, ...] = ("token", "authorization", "api_key", "password")


@dataclass(frozen=True, slots=True)
class SecurityConfig:
    """Security profile configuration (M7.1 #100).

    Defines operator-facing trust boundaries and validates incompatible
    permission/profile combinations. Profiles are NOT host-level sandbox
    guarantees — they describe intended use and reject obviously unsafe
    configurations.

    Profiles:
    - ``conservative`` (default): human-safer profile compatible with
      ``acceptEdits``; permission denials remain operator-visible through
      terminal outcome gates.
    - ``trusted_unattended``: intended for trusted repos/issues on trusted
      hosts; allows unattended work and may use ``bypassPermissions`` when
      explicitly configured (emits high-risk warning).
    - ``restricted``: read-only / no privileged tool posture; rejects
      ``bypassPermissions``; task completion may require handoff or blocked
      outcomes.
    """

    profile: str = "conservative"


@dataclass(frozen=True, slots=True)
class WorkflowConfig:
    tracker: TrackerConfig
    agent: AgentConfig
    workspace: WorkspaceConfig
    claude: ClaudeConfig
    github: GitHubConfig
    security: SecurityConfig = field(default_factory=SecurityConfig)
    polling: PollingConfig = field(default_factory=PollingConfig)
    retry: RetryConfig = field(default_factory=RetryConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    remote: RemoteConfig = field(default_factory=RemoteConfig)
    workflow_path: Path | None = None
    warnings: tuple[ConfigWarning, ...] = ()


# -- Helpers -------------------------------------------------------------------


def _resolve_env(value: Any, location: str, env: dict[str, str]) -> Any:
    """Resolve a scalar ``$ENV_VAR`` reference from ``env``.

    Only full-string ``$NAME`` substitutions are supported (matching the
    SPEC §7.1 ``token: $GITHUB_TOKEN`` example). Mid-string interpolation is
    intentionally NOT supported — it would invite YAML quoting bugs and
    silent partial-substitution, neither of which is worth the convenience.

    Non-string values pass through unchanged.
    """
    if not isinstance(value, str) or not value.startswith("$"):
        return value
    name = value[1:]
    if not name or not name.replace("_", "").isalnum():
        # Looks like a $-prefixed string that isn't an env reference (e.g.
        # ``"$5 fee"``); leave it alone rather than guessing.
        return value
    try:
        return env[name]
    except KeyError as exc:
        raise ConfigError(location, f"environment variable ${name} is not set") from exc


def _require_section(raw: dict[str, Any], name: str) -> dict[str, Any]:
    section = raw.get(name)
    if section is None:
        raise ConfigError(name, "required section is missing")
    if not isinstance(section, dict):
        raise ConfigError(name, f"must be a mapping, got {type(section).__name__}")
    return section


def _require_str(d: dict[str, Any], key: str, location: str) -> str:
    if key not in d or d[key] is None:
        raise ConfigError(f"{location}.{key}", "required field is missing")
    if not isinstance(d[key], str):
        raise ConfigError(f"{location}.{key}", f"must be a string, got {type(d[key]).__name__}")
    return d[key]


def _opt_str(d: dict[str, Any], key: str, location: str, default: str | None = None) -> str | None:
    if key not in d or d[key] is None:
        return default
    if not isinstance(d[key], str):
        raise ConfigError(f"{location}.{key}", f"must be a string, got {type(d[key]).__name__}")
    return d[key]


def _opt_bool(d: dict[str, Any], key: str, location: str, default: bool) -> bool:
    if key not in d or d[key] is None:
        return default
    if not isinstance(d[key], bool):
        raise ConfigError(f"{location}.{key}", f"must be a bool, got {type(d[key]).__name__}")
    return d[key]


def _opt_int(d: dict[str, Any], key: str, location: str, default: int) -> int:
    if key not in d or d[key] is None:
        return default
    # Reject bools (they are int subclasses in Python and would silently
    # pass an isinstance check).
    if isinstance(d[key], bool) or not isinstance(d[key], int):
        raise ConfigError(f"{location}.{key}", f"must be an int, got {type(d[key]).__name__}")
    return d[key]


def _opt_float(d: dict[str, Any], key: str, location: str, default: float) -> float:
    if key not in d or d[key] is None:
        return default
    if isinstance(d[key], bool) or not isinstance(d[key], (int, float)):
        raise ConfigError(f"{location}.{key}", f"must be a number, got {type(d[key]).__name__}")
    return float(d[key])


def _opt_str_list(
    d: dict[str, Any], key: str, location: str, default: tuple[str, ...] = ()
) -> tuple[str, ...]:
    if key not in d or d[key] is None:
        return default
    raw = d[key]
    if not isinstance(raw, list):
        raise ConfigError(f"{location}.{key}", f"must be a list, got {type(raw).__name__}")
    out: list[str] = []
    for i, item in enumerate(raw):
        if not isinstance(item, str):
            raise ConfigError(
                f"{location}.{key}[{i}]",
                f"must be a string, got {type(item).__name__}",
            )
        out.append(item)
    return tuple(out)


def _resolve_path(value: str, base: Path) -> Path:
    p = Path(value)
    if p.is_absolute():
        return p
    return (base / p).resolve()


# -- Builders ------------------------------------------------------------------


def _build_tracker(raw: dict[str, Any], env: dict[str, str]) -> TrackerConfig:
    section = _require_section(raw, "tracker")
    location = "tracker"
    kind = _require_str(section, "kind", location)
    if kind != "github":
        raise ConfigError(f"{location}.kind", f"must be 'github' (got {kind!r})")
    owner = _require_str(section, "owner", location)
    repo = _require_str(section, "repo", location)
    token_raw = _require_str(section, "token", location)
    token = _resolve_env(token_raw, f"{location}.token", env)
    if not token:
        raise ConfigError(f"{location}.token", "must be a non-empty string after env resolution")
    return TrackerConfig(
        kind=kind,
        owner=owner,
        repo=repo,
        token=token,
        include_labels=_opt_str_list(section, "include_labels", location),
        exclude_labels=_opt_str_list(section, "exclude_labels", location),
        terminal_labels=_opt_str_list(section, "terminal_labels", location),
        active_states=_opt_str_list(section, "active_states", location, default=("open",)),
    )


def _build_agent(raw: dict[str, Any]) -> AgentConfig:
    section = _require_section(raw, "agent")
    location = "agent"
    provider = _require_str(section, "provider", location)
    if provider != "claude_code":
        raise ConfigError(f"{location}.provider", f"must be 'claude_code' (got {provider!r})")
    max_concurrency = _opt_int(section, "max_concurrency", location, default=1)
    if max_concurrency < 1:
        raise ConfigError(f"{location}.max_concurrency", "must be >= 1")
    max_turns = _opt_int(section, "max_turns", location, default=3)
    if max_turns < 1:
        raise ConfigError(f"{location}.max_turns", "must be >= 1")
    tools = _build_agent_tools(section.get("tools") or {}, location=f"{location}.tools")
    return AgentConfig(
        provider=provider,
        max_concurrency=max_concurrency,
        max_turns=max_turns,
        tools=tools,
    )


def _build_agent_tools(raw: Any, *, location: str) -> AgentToolsConfig:
    """Build the optional ``agent.tools`` subtree.

    Permissive on absence (the default ``AgentToolsConfig`` has every
    tool disabled); strict on shape so a typo like ``github_graphq``
    fails fast at workflow load instead of being silently ignored.
    """
    if not isinstance(raw, dict):
        raise ConfigError(location, f"must be a mapping, got {type(raw).__name__}")
    gql_raw = raw.get("github_graphql") or {}
    if not isinstance(gql_raw, dict):
        raise ConfigError(
            f"{location}.github_graphql",
            f"must be a mapping, got {type(gql_raw).__name__}",
        )
    return AgentToolsConfig(
        github_graphql=GitHubGraphQLToolConfig(
            enabled=_opt_bool(
                gql_raw, "enabled", f"{location}.github_graphql", default=False
            ),
        ),
    )


def _build_workspace(raw: dict[str, Any], base_dir: Path) -> WorkspaceConfig:
    section = _require_section(raw, "workspace")
    location = "workspace"
    root_raw = _require_str(section, "root", location)
    populate = _opt_str(section, "populate", location, default="git")
    if populate != "git":
        raise ConfigError(
            f"{location}.populate",
            f"only 'git' is supported in this implementation (got {populate!r})",
        )
    cleanup = _build_workspace_cleanup(
        section.get("cleanup") or {}, location=f"{location}.cleanup"
    )
    return WorkspaceConfig(
        root=_resolve_path(root_raw, base_dir),
        populate=populate,
        remote=_opt_str(section, "remote", location, default="origin") or "origin",
        after_create=_opt_str(section, "after_create", location),
        before_run=_opt_str(section, "before_run", location),
        after_run=_opt_str(section, "after_run", location),
        before_delete=_opt_str(section, "before_delete", location),
        hook_timeout_ms=_opt_int(section, "hook_timeout_ms", location, default=300_000),
        cleanup=cleanup,
    )


def _build_workspace_cleanup(raw: Any, *, location: str) -> WorkspaceCleanupConfig:
    """Build the optional ``workspace.cleanup`` subtree (M5.6 #65).

    Permissive on absence (defaults ``enabled=False``). Known keys are
    strict on type/shape, but unknown keys are ignored by the shared
    ``_opt_*`` helper pattern used across config sections. Validates the
    enabled-with-no-trigger combo that #66's executor would treat as a
    no-op — reject at load time so operators don't ship a config that
    never deletes anything.
    """
    if not isinstance(raw, dict):
        raise ConfigError(location, f"must be a mapping, got {type(raw).__name__}")
    enabled = _opt_bool(raw, "enabled", location, default=False)
    on_terminal_issue = _opt_bool(raw, "on_terminal_issue", location, default=False)
    on_closed_pr = _opt_bool(raw, "on_closed_pr", location, default=False)
    max_age_days = _opt_int(raw, "max_age_days", location, default=0) or None
    if max_age_days is not None and max_age_days < 1:
        raise ConfigError(
            f"{location}.max_age_days",
            f"must be >= 1 when set, got {max_age_days}",
        )
    dry_run = _opt_bool(raw, "dry_run", location, default=False)
    if enabled and not (on_terminal_issue or on_closed_pr or max_age_days is not None):
        raise ConfigError(
            location,
            (
                "enabled=true requires at least one trigger: set "
                "on_terminal_issue, on_closed_pr, or max_age_days. An "
                "enabled cleanup with no trigger would never delete "
                "anything — drop enabled or add a trigger."
            ),
        )
    return WorkspaceCleanupConfig(
        enabled=enabled,
        on_terminal_issue=on_terminal_issue,
        on_closed_pr=on_closed_pr,
        max_age_days=max_age_days,
        dry_run=dry_run,
    )


def _build_artifact_retention(raw: Any, *, location: str) -> ArtifactRetentionConfig:
    """Build the optional ``claude.artifact_retention`` subtree (M5.6 #65).

    Same defensive shape as :func:`_build_workspace_cleanup` but with a
    narrower trigger set — artifacts are audit evidence so only age
    counts (per leader requirement on #65). Same enabled-with-no-trigger
    rejection.
    """
    if not isinstance(raw, dict):
        raise ConfigError(location, f"must be a mapping, got {type(raw).__name__}")
    enabled = _opt_bool(raw, "enabled", location, default=False)
    max_age_days = _opt_int(raw, "max_age_days", location, default=0) or None
    if max_age_days is not None and max_age_days < 1:
        raise ConfigError(
            f"{location}.max_age_days",
            f"must be >= 1 when set, got {max_age_days}",
        )
    dry_run = _opt_bool(raw, "dry_run", location, default=False)
    if enabled and max_age_days is None:
        raise ConfigError(
            location,
            (
                "enabled=true requires max_age_days. An enabled retention "
                "with no age trigger would never delete anything — drop "
                "enabled or set max_age_days."
            ),
        )
    return ArtifactRetentionConfig(
        enabled=enabled,
        max_age_days=max_age_days,
        dry_run=dry_run,
    )


def _build_claude(
    raw: dict[str, Any],
    base_dir: Path,
    *,
    warnings: list[ConfigWarning],
) -> ClaudeConfig:
    section = _require_section(raw, "claude")
    location = "claude"
    model = _require_str(section, "model", location)
    permission_mode = _require_str(section, "permission_mode", location)
    # plan mode is rejected outright (no human-in-the-loop). Other unknown
    # values land in the same error path. bypassPermissions is allowed but
    # gets a structured warning the CLI surfaces at startup.
    if permission_mode in REJECTED_PERMISSION_MODES:
        raise ConfigError(
            f"{location}.permission_mode",
            (
                f"{permission_mode!r} requires human confirmation and is not "
                f"supported by Symphony's unattended runtime "
                f"(see docs/claude-provider.md §7)"
            ),
        )
    if permission_mode not in ALLOWED_PERMISSION_MODES:
        raise ConfigError(
            f"{location}.permission_mode",
            f"must be one of {sorted(ALLOWED_PERMISSION_MODES)} (got {permission_mode!r})",
        )
    if permission_mode in WARN_PERMISSION_MODES:
        warnings.append(
            ConfigWarning(
                location=f"{location}.permission_mode",
                message=(
                    f"{permission_mode!r} disables Claude's interactive permission "
                    f"prompts; only enable in trusted local environments and review "
                    f"the workspace contents before granting it"
                ),
            )
        )
    session_store = _require_str(section, "session_store", location)
    transcript_store = _require_str(section, "transcript_store", location)
    artifact_store = _require_str(section, "artifact_store", location)
    policy = _opt_str(section, "retry_resume_policy", location, default="resume_same_session")
    if policy not in ALLOWED_RETRY_RESUME_POLICIES:
        raise ConfigError(
            f"{location}.retry_resume_policy",
            f"must be one of {sorted(ALLOWED_RETRY_RESUME_POLICIES)} (got {policy!r})",
        )
    return ClaudeConfig(
        model=model,
        permission_mode=permission_mode,
        session_store=_resolve_path(session_store, base_dir),
        transcript_store=_resolve_path(transcript_store, base_dir),
        artifact_store=_resolve_path(artifact_store, base_dir),
        turn_timeout_ms=_opt_int(section, "turn_timeout_ms", location, default=3_600_000),
        read_timeout_ms=_opt_int(section, "read_timeout_ms", location, default=30_000),
        stall_timeout_ms=_opt_int(section, "stall_timeout_ms", location, default=300_000),
        retry_resume_policy=policy,
        artifact_retention=_build_artifact_retention(
            section.get("artifact_retention") or {},
            location=f"{location}.artifact_retention",
        ),
    )


def _build_github(raw: dict[str, Any]) -> GitHubConfig:
    section = _require_section(raw, "github")
    location = "github"
    project_raw = section.get("project") or {}
    if not isinstance(project_raw, dict):
        raise ConfigError(
            f"{location}.project", f"must be a mapping, got {type(project_raw).__name__}"
        )
    project = GitHubProjectConfig(
        enabled=_opt_bool(project_raw, "enabled", f"{location}.project", default=False),
        owner=_opt_str(project_raw, "owner", f"{location}.project"),
        number=_opt_int(project_raw, "number", f"{location}.project", default=0) or None,
        status_field=_opt_str(project_raw, "status_field", f"{location}.project", default="Status")
        or "Status",
        ready_values=_opt_str_list(
            project_raw, "ready_values", f"{location}.project", default=("Ready",)
        ),
        running_value=_opt_str(
            project_raw, "running_value", f"{location}.project", default="In Progress"
        )
        or "In Progress",
        review_value=_opt_str(project_raw, "review_value", f"{location}.project", default="Review")
        or "Review",
    )
    if project.enabled and not project.owner:
        raise ConfigError(f"{location}.project.owner", "required when project.enabled is true")
    if project.enabled and not project.number:
        raise ConfigError(f"{location}.project.number", "required when project.enabled is true")
    return GitHubConfig(
        claim_label=_opt_str(section, "claim_label", location, default="symphony-running")
        or "symphony-running",
        ready_label=_opt_str(section, "ready_label", location, default="symphony-ready")
        or "symphony-ready",
        blocked_label=_opt_str(section, "blocked_label", location, default="symphony-blocked")
        or "symphony-blocked",
        done_label=_opt_str(section, "done_label", location, default="symphony-done")
        or "symphony-done",
        branch_prefix=_opt_str(section, "branch_prefix", location, default="symphony")
        or "symphony",
        base_branch=_opt_str(section, "base_branch", location, default="main") or "main",
        draft_pr=_opt_bool(section, "draft_pr", location, default=True),
        claim_comment=_opt_bool(section, "claim_comment", location, default=True),
        pr_link_comment=_opt_bool(section, "pr_link_comment", location, default=True),
        close_issue_on_done=_opt_bool(section, "close_issue_on_done", location, default=False),
        project=project,
    )


def _build_polling(raw: dict[str, Any]) -> PollingConfig:
    section = raw.get("polling") or {}
    if not isinstance(section, dict):
        raise ConfigError("polling", f"must be a mapping, got {type(section).__name__}")
    return PollingConfig(
        interval_ms=_opt_int(section, "interval_ms", "polling", default=60_000),
        reconcile_interval_ms=_opt_int(section, "reconcile_interval_ms", "polling", default=30_000),
    )


def _build_retry(raw: dict[str, Any]) -> RetryConfig:
    section = raw.get("retry") or {}
    if not isinstance(section, dict):
        raise ConfigError("retry", f"must be a mapping, got {type(section).__name__}")
    return RetryConfig(
        max_attempts=_opt_int(section, "max_attempts", "retry", default=3),
        initial_backoff_ms=_opt_int(section, "initial_backoff_ms", "retry", default=60_000),
        max_backoff_ms=_opt_int(section, "max_backoff_ms", "retry", default=900_000),
        multiplier=_opt_float(section, "multiplier", "retry", default=2.0),
    )


def _build_logging(raw: dict[str, Any], base_dir: Path) -> LoggingConfig:
    section = raw.get("logging") or {}
    if not isinstance(section, dict):
        raise ConfigError("logging", f"must be a mapping, got {type(section).__name__}")
    jsonl_raw = _opt_str(section, "jsonl_path", "logging")
    return LoggingConfig(
        level=_opt_str(section, "level", "logging", default="info") or "info",
        jsonl_path=_resolve_path(jsonl_raw, base_dir) if jsonl_raw else None,
        redact_keys=_opt_str_list(
            section,
            "redact_keys",
            "logging",
            default=("token", "authorization", "api_key", "password"),
        ),
    )


def _build_security(raw: dict[str, Any]) -> SecurityConfig:
    """Build SecurityConfig from optional 'security' section.

    Defaults to 'conservative' profile if section is missing or profile
    is unspecified.
    """
    section = raw.get("security") or {}
    if not isinstance(section, dict):
        raise ConfigError("security", f"must be a mapping, got {type(section).__name__}")
    profile = _opt_str(section, "profile", "security", default="conservative") or "conservative"
    if profile not in ALLOWED_SECURITY_PROFILES:
        raise ConfigError(
            "security.profile",
            f"must be one of {sorted(ALLOWED_SECURITY_PROFILES)} (got {profile!r})",
        )
    return SecurityConfig(profile=profile)


def _build_remote(raw: dict[str, Any]) -> RemoteConfig:
    """Build RemoteConfig from optional 'remote' section.

    Remote execution is disabled by default. When enabled, required fields
    (host, workspace_root, artifact_root, session_store) must be present.
    """
    location = "remote"
    section = raw.get(location) or {}
    if not isinstance(section, dict):
        raise ConfigError(location, f"must be a mapping, got {type(section).__name__}")

    enabled = _opt_bool(section, "enabled", location, default=False)
    host = _opt_str(section, "host", location)
    workspace_root = _opt_str(section, "workspace_root", location)
    artifact_root = _opt_str(section, "artifact_root", location)
    session_store = _opt_str(section, "session_store", location)
    git_token = _opt_str(section, "git_token", location)
    worker_timeout_ms = _opt_int(
        section, "worker_timeout_ms", location, default=7_200_000
    )
    heartbeat_interval_ms = _opt_int(
        section, "heartbeat_interval_ms", location, default=30_000
    )
    stall_timeout_ms = _opt_int(section, "stall_timeout_ms", location, default=300_000)

    for key, value in (
        ("worker_timeout_ms", worker_timeout_ms),
        ("heartbeat_interval_ms", heartbeat_interval_ms),
        ("stall_timeout_ms", stall_timeout_ms),
    ):
        if value < 1:
            raise ConfigError(f"{location}.{key}", "must be >= 1")

    if heartbeat_interval_ms >= stall_timeout_ms:
        raise ConfigError(
            f"{location}.heartbeat_interval_ms",
            "must be less than remote.stall_timeout_ms",
        )
    if stall_timeout_ms > worker_timeout_ms:
        raise ConfigError(
            f"{location}.stall_timeout_ms",
            "must be <= remote.worker_timeout_ms",
        )

    if enabled:
        for key, value in (
            ("host", host),
            ("workspace_root", workspace_root),
            ("artifact_root", artifact_root),
            ("session_store", session_store),
        ):
            if not value:
                raise ConfigError(f"{location}.{key}", "required when remote.enabled is true")

    return RemoteConfig(
        enabled=enabled,
        host=host,
        workspace_root=workspace_root,
        artifact_root=artifact_root,
        session_store=session_store,
        git_token=git_token,
        worker_timeout_ms=worker_timeout_ms,
        heartbeat_interval_ms=heartbeat_interval_ms,
        stall_timeout_ms=stall_timeout_ms,
    )


# -- Public entry point -------------------------------------------------------


def build_config(
    raw: dict[str, Any],
    *,
    workflow_path: Path,
    env: dict[str, str] | None = None,
) -> WorkflowConfig:
    """Build a typed :class:`WorkflowConfig` from a parsed YAML mapping.

    ``workflow_path`` is the absolute path to ``WORKFLOW.md``. It is used as
    the base directory when resolving relative paths in ``workspace.root``,
    ``claude.session_store``, ``claude.transcript_store``, ``claude.artifact_store``,
    and ``logging.jsonl_path``.

    ``env`` defaults to ``os.environ`` and is exposed as a parameter so tests
    can pass a controlled mapping without monkey-patching.
    """
    if env is None:
        env = dict(os.environ)
    if not isinstance(raw, dict):
        raise ConfigError(
            "(root)", f"workflow front matter must be a mapping, got {type(raw).__name__}"
        )

    base_dir = workflow_path.parent.resolve()

    missing = [name for name in REQUIRED_SECTIONS if name not in raw]
    if missing:
        raise ConfigError("(root)", f"missing required sections: {', '.join(missing)}")

    warnings: list[ConfigWarning] = []
    config = WorkflowConfig(
        tracker=_build_tracker(raw, env),
        agent=_build_agent(raw),
        workspace=_build_workspace(raw, base_dir),
        claude=_build_claude(raw, base_dir, warnings=warnings),
        github=_build_github(raw),
        security=_build_security(raw),
        polling=_build_polling(raw),
        retry=_build_retry(raw),
        logging=_build_logging(raw, base_dir),
        remote=_build_remote(raw),
        workflow_path=workflow_path.resolve(),
        warnings=tuple(warnings),
    )

    # M7.1 #100: Cross-field validation for security profile + permission_mode.
    _validate_security_profile(config, warnings)

    # Re-freeze warnings after cross-field validation may have added more.
    config = replace(config, warnings=tuple(warnings))
    return config


def _validate_security_profile(config: WorkflowConfig, warnings: list[ConfigWarning]) -> None:
    """Validate security profile against permission_mode (M7.1 #100).

    - ``restricted`` + ``bypassPermissions`` is a config error.
    - ``trusted_unattended`` + ``bypassPermissions`` is allowed but emits
      a high-risk warning (in addition to the existing bypassPermissions warning).
    - ``conservative`` + ``bypassPermissions`` keeps the existing warning.
    """
    profile = config.security.profile
    permission_mode = config.claude.permission_mode

    if profile == "restricted" and permission_mode == "bypassPermissions":
        raise ConfigError(
            "security.profile",
            (
                "profile 'restricted' is incompatible with "
                "claude.permission_mode='bypassPermissions' - restricted profile "
                "requires permission prompts to remain active"
            ),
        )

    if profile == "trusted_unattended" and permission_mode == "bypassPermissions":
        warnings.append(
            ConfigWarning(
                location="security.profile",
                message=(
                    "profile 'trusted_unattended' with bypassPermissions disables all "
                    "interactive permission checks; only use in fully trusted environments "
                    "with trusted repos and issues"
                ),
            )
        )


def with_overrides(config: WorkflowConfig, **overrides: Any) -> WorkflowConfig:
    """Return a copy of ``config`` with top-level fields overridden.

    Reserved for tests and tooling; runtime should not mutate config.
    """
    return replace(config, **overrides)
