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


# -- Constants -----------------------------------------------------------------

REQUIRED_SECTIONS: tuple[str, ...] = (
    "tracker",
    "agent",
    "workspace",
    "claude",
    "github",
)

ALLOWED_PERMISSION_MODES: frozenset[str] = frozenset(
    {"default", "acceptEdits", "plan", "bypassPermissions", "dontAsk", "auto"}
)

ALLOWED_RETRY_RESUME_POLICIES: frozenset[str] = frozenset(
    {"resume_same_session", "new_session_with_summary", "fail_closed"}
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
class AgentConfig:
    provider: str = "claude_code"
    max_concurrency: int = 1
    max_turns: int = 3


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
class WorkflowConfig:
    tracker: TrackerConfig
    agent: AgentConfig
    workspace: WorkspaceConfig
    claude: ClaudeConfig
    github: GitHubConfig
    polling: PollingConfig = field(default_factory=PollingConfig)
    retry: RetryConfig = field(default_factory=RetryConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    workflow_path: Path | None = None


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
    return AgentConfig(
        provider=provider,
        max_concurrency=max_concurrency,
        max_turns=max_turns,
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
    return WorkspaceConfig(
        root=_resolve_path(root_raw, base_dir),
        populate=populate,
        remote=_opt_str(section, "remote", location, default="origin") or "origin",
        after_create=_opt_str(section, "after_create", location),
        before_run=_opt_str(section, "before_run", location),
        after_run=_opt_str(section, "after_run", location),
        before_delete=_opt_str(section, "before_delete", location),
        hook_timeout_ms=_opt_int(section, "hook_timeout_ms", location, default=300_000),
    )


def _build_claude(raw: dict[str, Any], base_dir: Path) -> ClaudeConfig:
    section = _require_section(raw, "claude")
    location = "claude"
    model = _require_str(section, "model", location)
    permission_mode = _require_str(section, "permission_mode", location)
    if permission_mode not in ALLOWED_PERMISSION_MODES:
        raise ConfigError(
            f"{location}.permission_mode",
            f"must be one of {sorted(ALLOWED_PERMISSION_MODES)} (got {permission_mode!r})",
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

    config = WorkflowConfig(
        tracker=_build_tracker(raw, env),
        agent=_build_agent(raw),
        workspace=_build_workspace(raw, base_dir),
        claude=_build_claude(raw, base_dir),
        github=_build_github(raw),
        polling=_build_polling(raw),
        retry=_build_retry(raw),
        logging=_build_logging(raw, base_dir),
        workflow_path=workflow_path.resolve(),
    )
    return config


def with_overrides(config: WorkflowConfig, **overrides: Any) -> WorkflowConfig:
    """Return a copy of ``config`` with top-level fields overridden.

    Reserved for tests and tooling; runtime should not mutate config.
    """
    return replace(config, **overrides)
