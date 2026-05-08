"""Tests for workflow loading, config validation, and prompt rendering.

Covers SPEC.md §6 (Workflow File), §7 (Configuration), and the
acceptance criteria on issue #5.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from symphony.config import (
    ALLOWED_PERMISSION_MODES,
    ALLOWED_RETRY_RESUME_POLICIES,
    ConfigError,
    GitHubConfig,
    GitHubProjectConfig,
    LoggingConfig,
    PollingConfig,
    RetryConfig,
    WorkflowConfig,
    build_config,
)
from symphony.models import Issue
from symphony.workflow import WorkflowError, load_workflow, render_prompt

FIXTURES = Path(__file__).parent / "fixtures" / "workflows"


# -- Fixtures -----------------------------------------------------------------


@pytest.fixture
def sample_issue() -> Issue:
    return Issue(
        id="I_kw1",
        number=42,
        identifier="jimoosciuc/symphony-cc#42",
        owner="jimoosciuc",
        repo="symphony-cc",
        title="Add cool feature",
        body="Please do the thing.",
        state="open",
        url="https://github.com/jimoosciuc/symphony-cc/issues/42",
    )


@pytest.fixture
def env_with_token() -> dict[str, str]:
    return {"GITHUB_TOKEN": "ghp_test_12345"}


# -- Loader: happy path -------------------------------------------------------


def test_loads_valid_example_repo_workflow(env_with_token: dict[str, str]) -> None:
    """Acceptance: the example workflow that ships with the repo loads."""
    workflow = load_workflow(Path("WORKFLOW.example.md"), env=env_with_token)
    assert workflow.config.tracker.owner == "jimoosciuc"
    assert workflow.config.tracker.repo == "symphony-cc"
    assert workflow.config.tracker.token == "ghp_test_12345"
    assert workflow.config.agent.provider == "claude_code"
    assert workflow.config.claude.permission_mode == "acceptEdits"
    assert "issue.identifier" in workflow.prompt_template


def test_loads_fixture_workflow(env_with_token: dict[str, str]) -> None:
    workflow = load_workflow(FIXTURES / "valid.md", env=env_with_token)
    assert workflow.config.agent.max_concurrency == 2
    assert workflow.config.agent.max_turns == 5
    # Fixtures live under tests/fixtures/workflows; relative paths must
    # resolve against THAT directory, not the test process cwd.
    assert workflow.config.workspace.root.is_absolute()
    assert workflow.config.workspace.root.parent == ((FIXTURES / ".symphony").resolve())


# -- Loader: env var resolution ----------------------------------------------


def test_env_var_resolution_substitutes_scalar(env_with_token: dict[str, str]) -> None:
    workflow = load_workflow(FIXTURES / "valid.md", env=env_with_token)
    assert workflow.config.tracker.token == "ghp_test_12345"


def test_missing_env_var_raises_clear_error() -> None:
    with pytest.raises(ConfigError) as excinfo:
        load_workflow(FIXTURES / "missing_env.md", env={})
    assert excinfo.value.location == "tracker.token"
    assert "SYMPHONY_TEST_NONEXISTENT_TOKEN" in str(excinfo.value)


def test_dollar_prefixed_non_envvar_passes_through() -> None:
    """A literal '$5 fee' shouldn't be misread as an env reference."""
    raw = {
        "tracker": {
            "kind": "github",
            "owner": "o",
            "repo": "r",
            "token": "$5 fee",
        },
        "agent": {"provider": "claude_code"},
        "workspace": {"root": "ws"},
        "claude": {
            "model": "claude-opus-4-7",
            "permission_mode": "acceptEdits",
            "session_store": "s",
            "transcript_store": "t",
            "artifact_store": "a",
        },
        "github": {},
    }
    cfg = build_config(raw, workflow_path=Path("/tmp/WORKFLOW.md"), env={})
    assert cfg.tracker.token == "$5 fee"


# -- Loader: required sections ------------------------------------------------


def test_missing_required_sections_lists_all_at_once() -> None:
    with pytest.raises(ConfigError) as excinfo:
        load_workflow(FIXTURES / "missing_sections.md", env={"GITHUB_TOKEN": "x"})
    assert excinfo.value.location == "(root)"
    msg = str(excinfo.value)
    # The fixture only provides `agent`; the other four required sections
    # should all be reported in a single message.
    for section in ("tracker", "workspace", "claude", "github"):
        assert section in msg


def test_missing_file_raises_workflow_error(tmp_path: Path) -> None:
    missing = tmp_path / "nope.md"
    with pytest.raises(WorkflowError) as excinfo:
        load_workflow(missing)
    assert str(missing) in excinfo.value.location


# -- Loader: front-matter parsing ---------------------------------------------


def test_workflow_without_front_matter_fails(tmp_path: Path) -> None:
    f = tmp_path / "noyaml.md"
    f.write_text("Just a prompt, no front matter.\n", encoding="utf-8")
    with pytest.raises(WorkflowError) as excinfo:
        load_workflow(f)
    assert "front-matter" in str(excinfo.value).lower()


def test_workflow_missing_closing_delimiter_fails(tmp_path: Path) -> None:
    f = tmp_path / "noclose.md"
    f.write_text("---\nagent:\n  provider: claude_code\n", encoding="utf-8")
    with pytest.raises(WorkflowError) as excinfo:
        load_workflow(f)
    assert "closing" in str(excinfo.value).lower()


def test_invalid_yaml_in_front_matter_fails(tmp_path: Path) -> None:
    f = tmp_path / "badyaml.md"
    f.write_text("---\nagent: [unterminated\n---\nbody\n", encoding="utf-8")
    with pytest.raises(WorkflowError) as excinfo:
        load_workflow(f)
    assert "invalid yaml" in str(excinfo.value).lower()


# -- Defaults: §7 coverage ----------------------------------------------------


def test_github_defaults_match_spec(env_with_token: dict[str, str]) -> None:
    workflow = load_workflow(FIXTURES / "valid.md", env=env_with_token)
    g: GitHubConfig = workflow.config.github
    assert g.claim_label == "symphony-running"
    assert g.ready_label == "symphony-ready"
    assert g.blocked_label == "symphony-blocked"
    assert g.done_label == "symphony-done"
    assert g.branch_prefix == "symphony"
    assert g.base_branch == "main"
    assert g.draft_pr is True
    assert g.claim_comment is True
    assert g.pr_link_comment is True
    assert g.close_issue_on_done is False
    assert isinstance(g.project, GitHubProjectConfig)
    assert g.project.enabled is False


def test_claude_defaults_match_spec(env_with_token: dict[str, str]) -> None:
    workflow = load_workflow(FIXTURES / "valid.md", env=env_with_token)
    c = workflow.config.claude
    assert c.turn_timeout_ms == 3_600_000
    assert c.read_timeout_ms == 30_000
    assert c.stall_timeout_ms == 300_000
    assert c.retry_resume_policy == "resume_same_session"


def test_workspace_defaults_match_spec(env_with_token: dict[str, str]) -> None:
    workflow = load_workflow(FIXTURES / "valid.md", env=env_with_token)
    w = workflow.config.workspace
    assert w.populate == "git"
    assert w.remote == "origin"
    assert w.hook_timeout_ms == 300_000
    assert w.before_run is None
    assert w.after_run is None


def test_polling_retry_logging_defaults_when_omitted(env_with_token: dict[str, str]) -> None:
    workflow = load_workflow(FIXTURES / "valid.md", env=env_with_token)
    assert workflow.config.polling == PollingConfig(
        interval_ms=60_000, reconcile_interval_ms=30_000
    )
    assert workflow.config.retry == RetryConfig(
        max_attempts=3,
        initial_backoff_ms=60_000,
        max_backoff_ms=900_000,
        multiplier=2.0,
    )
    assert workflow.config.logging == LoggingConfig(
        level="info",
        jsonl_path=None,
        redact_keys=("token", "authorization", "api_key", "password"),
    )


def test_remote_config_defaults_disabled() -> None:
    raw = _minimal_raw()
    cfg = build_config(raw, workflow_path=Path("/tmp/W.md"), env={})
    assert cfg.remote.enabled is False
    assert cfg.remote.host is None
    assert cfg.remote.workspace_root is None
    assert cfg.remote.artifact_root is None
    assert cfg.remote.session_store is None
    assert cfg.remote.worker_timeout_ms == 7_200_000
    assert cfg.remote.heartbeat_interval_ms == 30_000
    assert cfg.remote.stall_timeout_ms == 300_000


def test_remote_disabled_allows_missing_remote_roots() -> None:
    raw = _minimal_raw()
    raw["remote"] = {"enabled": False}
    cfg = build_config(raw, workflow_path=Path("/tmp/W.md"), env={})
    assert cfg.remote.enabled is False
    assert cfg.remote.workspace_root is None


def test_remote_enabled_valid_config_preserves_remote_paths() -> None:
    raw = _minimal_raw()
    raw["remote"] = {
        "enabled": True,
        "host": "builder-1",
        "workspace_root": "relative/remote/ws",
        "artifact_root": "/srv/symphony/artifacts",
        "session_store": "relative/remote/sessions",
        "worker_timeout_ms": 600_000,
        "heartbeat_interval_ms": 10_000,
        "stall_timeout_ms": 60_000,
    }
    cfg = build_config(raw, workflow_path=Path("/tmp/workflows/W.md"), env={})
    assert cfg.remote.enabled is True
    assert cfg.remote.host == "builder-1"
    assert cfg.remote.workspace_root == "relative/remote/ws"
    assert cfg.remote.artifact_root == "/srv/symphony/artifacts"
    assert cfg.remote.session_store == "relative/remote/sessions"
    assert cfg.remote.worker_timeout_ms == 600_000
    assert cfg.remote.heartbeat_interval_ms == 10_000
    assert cfg.remote.stall_timeout_ms == 60_000


def test_remote_section_must_be_mapping() -> None:
    raw = _minimal_raw()
    raw["remote"] = True
    with pytest.raises(ConfigError) as excinfo:
        build_config(raw, workflow_path=Path("/tmp/W.md"), env={})
    assert excinfo.value.location == "remote"


@pytest.mark.parametrize(
    ("field", "location"),
    [
        ("host", "remote.host"),
        ("workspace_root", "remote.workspace_root"),
        ("artifact_root", "remote.artifact_root"),
        ("session_store", "remote.session_store"),
    ],
)
def test_remote_enabled_requires_fields(field: str, location: str) -> None:
    raw = _minimal_raw()
    raw["remote"] = {
        "enabled": True,
        "host": "builder-1",
        "workspace_root": "/srv/ws",
        "artifact_root": "/srv/artifacts",
        "session_store": "/srv/sessions",
    }
    del raw["remote"][field]
    with pytest.raises(ConfigError) as excinfo:
        build_config(raw, workflow_path=Path("/tmp/W.md"), env={})
    assert excinfo.value.location == location


@pytest.mark.parametrize(
    "field",
    ["worker_timeout_ms", "heartbeat_interval_ms", "stall_timeout_ms"],
)
def test_remote_timeout_fields_must_be_ints(field: str) -> None:
    raw = _minimal_raw()
    raw["remote"] = {field: "1000"}
    with pytest.raises(ConfigError) as excinfo:
        build_config(raw, workflow_path=Path("/tmp/W.md"), env={})
    assert excinfo.value.location == f"remote.{field}"


@pytest.mark.parametrize(
    "field",
    ["worker_timeout_ms", "heartbeat_interval_ms", "stall_timeout_ms"],
)
def test_remote_timeout_fields_must_be_positive(field: str) -> None:
    raw = _minimal_raw()
    raw["remote"] = {field: 0}
    with pytest.raises(ConfigError) as excinfo:
        build_config(raw, workflow_path=Path("/tmp/W.md"), env={})
    assert excinfo.value.location == f"remote.{field}"


def test_remote_heartbeat_must_be_less_than_stall() -> None:
    raw = _minimal_raw()
    raw["remote"] = {
        "heartbeat_interval_ms": 60_000,
        "stall_timeout_ms": 60_000,
    }
    with pytest.raises(ConfigError) as excinfo:
        build_config(raw, workflow_path=Path("/tmp/W.md"), env={})
    assert excinfo.value.location == "remote.heartbeat_interval_ms"


def test_remote_stall_must_not_exceed_worker_timeout() -> None:
    raw = _minimal_raw()
    raw["remote"] = {
        "worker_timeout_ms": 30_000,
        "heartbeat_interval_ms": 10_000,
        "stall_timeout_ms": 60_000,
    }
    with pytest.raises(ConfigError) as excinfo:
        build_config(raw, workflow_path=Path("/tmp/W.md"), env={})
    assert excinfo.value.location == "remote.stall_timeout_ms"


# -- Validation: type and value constraints -----------------------------------


def _minimal_raw() -> dict[str, object]:
    return {
        "tracker": {
            "kind": "github",
            "owner": "o",
            "repo": "r",
            "token": "literal-token",
        },
        "agent": {"provider": "claude_code"},
        "workspace": {"root": "ws"},
        "claude": {
            "model": "claude-opus-4-7",
            "permission_mode": "acceptEdits",
            "session_store": "s",
            "transcript_store": "t",
            "artifact_store": "a",
        },
        "github": {},
    }


def test_tracker_kind_must_be_github() -> None:
    raw = _minimal_raw()
    raw["tracker"]["kind"] = "linear"
    with pytest.raises(ConfigError) as excinfo:
        build_config(raw, workflow_path=Path("/tmp/W.md"), env={})
    assert excinfo.value.location == "tracker.kind"


def test_agent_provider_must_be_claude_code() -> None:
    raw = _minimal_raw()
    raw["agent"]["provider"] = "codex"
    with pytest.raises(ConfigError) as excinfo:
        build_config(raw, workflow_path=Path("/tmp/W.md"), env={})
    assert excinfo.value.location == "agent.provider"


@pytest.mark.parametrize("mode", sorted(ALLOWED_PERMISSION_MODES))
def test_all_accepted_permission_modes_pass(mode: str) -> None:
    raw = _minimal_raw()
    raw["claude"]["permission_mode"] = mode
    cfg = build_config(raw, workflow_path=Path("/tmp/W.md"), env={})
    assert cfg.claude.permission_mode == mode


def test_plan_permission_mode_rejected() -> None:
    """Plan mode blocks on human confirmation; Symphony has no human in the
    loop, so the config layer rejects it up front rather than letting the
    provider stall on the first turn."""
    raw = _minimal_raw()
    raw["claude"]["permission_mode"] = "plan"
    with pytest.raises(ConfigError) as excinfo:
        build_config(raw, workflow_path=Path("/tmp/W.md"), env={})
    assert excinfo.value.location == "claude.permission_mode"
    assert "human" in str(excinfo.value).lower() or "unattended" in str(excinfo.value).lower()


def test_bypass_permissions_emits_warning() -> None:
    """`bypassPermissions` is allowed (operator opt-in) but must surface a
    structured warning the CLI can log once at startup."""
    raw = _minimal_raw()
    raw["claude"]["permission_mode"] = "bypassPermissions"
    cfg = build_config(raw, workflow_path=Path("/tmp/W.md"), env={})
    assert cfg.claude.permission_mode == "bypassPermissions"
    assert len(cfg.warnings) == 1
    w = cfg.warnings[0]
    assert w.location == "claude.permission_mode"
    assert "bypassPermissions" in w.message


def test_safe_permission_mode_emits_no_warning() -> None:
    raw = _minimal_raw()
    raw["claude"]["permission_mode"] = "acceptEdits"
    cfg = build_config(raw, workflow_path=Path("/tmp/W.md"), env={})
    assert cfg.warnings == ()


def test_unknown_permission_mode_rejected() -> None:
    raw = _minimal_raw()
    raw["claude"]["permission_mode"] = "yolo"
    with pytest.raises(ConfigError) as excinfo:
        build_config(raw, workflow_path=Path("/tmp/W.md"), env={})
    assert excinfo.value.location == "claude.permission_mode"


@pytest.mark.parametrize("policy", sorted(ALLOWED_RETRY_RESUME_POLICIES))
def test_all_documented_retry_policies_accepted(policy: str) -> None:
    raw = _minimal_raw()
    raw["claude"]["retry_resume_policy"] = policy
    cfg = build_config(raw, workflow_path=Path("/tmp/W.md"), env={})
    assert cfg.claude.retry_resume_policy == policy


def test_max_concurrency_must_be_positive() -> None:
    raw = _minimal_raw()
    raw["agent"]["max_concurrency"] = 0
    with pytest.raises(ConfigError) as excinfo:
        build_config(raw, workflow_path=Path("/tmp/W.md"), env={})
    assert excinfo.value.location == "agent.max_concurrency"


def test_bool_in_int_field_rejected() -> None:
    """Bools are int subclasses in Python; the validator must catch this."""
    raw = _minimal_raw()
    raw["agent"]["max_turns"] = True
    with pytest.raises(ConfigError) as excinfo:
        build_config(raw, workflow_path=Path("/tmp/W.md"), env={})
    assert excinfo.value.location == "agent.max_turns"


def test_project_enabled_requires_owner_and_number() -> None:
    raw = _minimal_raw()
    raw["github"]["project"] = {"enabled": True}
    with pytest.raises(ConfigError) as excinfo:
        build_config(raw, workflow_path=Path("/tmp/W.md"), env={})
    assert excinfo.value.location.startswith("github.project.")


# -- Path normalization -------------------------------------------------------


def test_relative_paths_resolve_relative_to_workflow_dir(tmp_path: Path) -> None:
    nested = tmp_path / "ops" / "WORKFLOW.md"
    nested.parent.mkdir(parents=True)
    nested.write_text(
        "---\n"
        "tracker: {kind: github, owner: o, repo: r, token: literal}\n"
        "agent: {provider: claude_code}\n"
        "workspace: {root: ./ws}\n"
        "claude: {model: m, permission_mode: acceptEdits, "
        "session_store: ./s, transcript_store: ./t, artifact_store: ./a}\n"
        "github: {}\n"
        "logging: {jsonl_path: ./logs/symphony.jsonl}\n"
        "---\n"
        "Body for {{ issue.identifier }}.\n",
        encoding="utf-8",
    )
    workflow = load_workflow(nested, env={})
    base = nested.parent.resolve()
    assert workflow.config.workspace.root == (base / "ws").resolve()
    assert workflow.config.claude.session_store == (base / "s").resolve()
    assert workflow.config.claude.transcript_store == (base / "t").resolve()
    assert workflow.config.claude.artifact_store == (base / "a").resolve()
    assert workflow.config.logging.jsonl_path == (base / "logs/symphony.jsonl").resolve()


def test_absolute_paths_kept_as_is(tmp_path: Path) -> None:
    abs_root = tmp_path / "absworkspaces"
    raw = _minimal_raw()
    raw["workspace"]["root"] = str(abs_root)
    cfg = build_config(raw, workflow_path=tmp_path / "W.md", env={})
    assert cfg.workspace.root == abs_root


# -- Prompt rendering ---------------------------------------------------------


def test_render_prompt_substitutes_issue_attrs(
    sample_issue: Issue, env_with_token: dict[str, str]
) -> None:
    workflow = load_workflow(FIXTURES / "valid.md", env=env_with_token)
    rendered = render_prompt(workflow, issue=sample_issue)
    assert "jimoosciuc/symphony-cc#42" in rendered
    assert "Add cool feature" in rendered
    assert sample_issue.url in rendered
    assert "open" in rendered


def test_render_prompt_extra_keys_available(
    sample_issue: Issue, env_with_token: dict[str, str], tmp_path: Path
) -> None:
    f = tmp_path / "W.md"
    f.write_text(
        "---\n"
        "tracker: {kind: github, owner: o, repo: r, token: literal}\n"
        "agent: {provider: claude_code}\n"
        "workspace: {root: ws}\n"
        "claude: {model: m, permission_mode: acceptEdits, "
        "session_store: s, transcript_store: t, artifact_store: a}\n"
        "github: {}\n"
        "---\n"
        "issue={{ issue.identifier }}\nws={{ workspace_path }}\n",
        encoding="utf-8",
    )
    wf = load_workflow(f, env={})
    out = render_prompt(wf, issue=sample_issue, extra={"workspace_path": "/tmp/abc"})
    assert "issue=jimoosciuc/symphony-cc#42" in out
    assert "ws=/tmp/abc" in out


def test_render_prompt_extra_cannot_shadow_issue(
    sample_issue: Issue, env_with_token: dict[str, str]
) -> None:
    """`extra={"issue": ...}` would silently shadow the positional issue arg.
    The renderer rejects it explicitly so callers get a clear error instead
    of confusing template output."""
    workflow = load_workflow(FIXTURES / "valid.md", env=env_with_token)
    with pytest.raises(WorkflowError) as excinfo:
        render_prompt(workflow, issue=sample_issue, extra={"issue": "fake"})
    assert excinfo.value.location == "prompt.extra"
    assert "issue" in str(excinfo.value)


def test_unknown_prompt_variable_fails_closed(
    sample_issue: Issue, env_with_token: dict[str, str]
) -> None:
    workflow = load_workflow(FIXTURES / "unknown_var.md", env=env_with_token)
    # The template reference is collected at load time…
    assert "unknown_thing" in workflow.referenced_variables
    # …and renders fail closed when not provided.
    with pytest.raises(WorkflowError) as excinfo:
        render_prompt(workflow, issue=sample_issue)
    assert excinfo.value.location == "prompt"


def test_unknown_filter_fails_closed_at_load(tmp_path: Path) -> None:
    """Acceptance: unknown filters fail closed.

    Jinja2 catches unknown filters during the AST walk that
    ``meta.find_undeclared_variables`` performs, so this fires at load
    time — earlier than the unknown-variable case, which is fine: the
    sooner we fail, the better.
    """
    f = tmp_path / "W.md"
    f.write_text(
        "---\n"
        "tracker: {kind: github, owner: o, repo: r, token: literal}\n"
        "agent: {provider: claude_code}\n"
        "workspace: {root: ws}\n"
        "claude: {model: m, permission_mode: acceptEdits, "
        "session_store: s, transcript_store: t, artifact_store: a}\n"
        "github: {}\n"
        "---\n"
        "{{ issue.identifier | reticulate }}\n",
        encoding="utf-8",
    )
    with pytest.raises(WorkflowError) as excinfo:
        load_workflow(f, env={})
    assert excinfo.value.location == "prompt"
    assert "reticulate" in str(excinfo.value)


# -- WorkflowConfig type hygiene ---------------------------------------------


def test_workflow_config_is_immutable(env_with_token: dict[str, str]) -> None:
    workflow = load_workflow(FIXTURES / "valid.md", env=env_with_token)
    cfg: WorkflowConfig = workflow.config
    with pytest.raises((AttributeError, TypeError)):
        # Frozen dataclass; mutation must not silently succeed.
        cfg.agent = None  # type: ignore[misc]
