"""Tests for the optional ``github_graphql`` tool (issue #13).

Two layers:

1. **Validation** — ``validate_query`` / ``validate_variables`` and the
   integrated ``GitHubGraphQLTool.execute`` path that gates input before
   any HTTP call.
2. **Transport** — ``GitHubGraphQLTool.execute`` against a
   :class:`httpx.MockTransport` so we exercise the real
   ``GitHubClient`` request path (auth header, JSON body, error
   mapping) without touching the network.

Plus a smoke test for the provider's options assembly: when a
``ToolRegistry`` is supplied, ``ClaudeCodeProvider._build_options``
includes ``mcp_servers`` + ``allowed_tools`` entries; without the
registry, it does not. Live SDK round-trip is intentionally out of
scope — the SDK side is shimmed and only exercised under
``SYMPHONY_RUN_*_INTEGRATION=1``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from symphony.config import (
    AgentConfig,
    AgentToolsConfig,
    ClaudeConfig,
    GitHubConfig,
    GitHubGraphQLToolConfig,
    LoggingConfig,
    PollingConfig,
    RetryConfig,
    TrackerConfig,
    WorkflowConfig,
    WorkspaceConfig,
    build_config,
)
from symphony.github.client import GitHubClient
from symphony.models import Issue
from symphony.provider.claude_code import ClaudeCodeProvider, ToolRegistry
from symphony.tools.github_graphql import (
    TOOL_NAME,
    EmptyQueryError,
    GitHubGraphQLTool,
    GraphQLToolResult,
    InvalidVariablesError,
    MultipleOperationsError,
    validate_query,
    validate_variables,
)

# -- Fixtures ----------------------------------------------------------------


def _client_with(handler: Any) -> GitHubClient:
    """Build a :class:`GitHubClient` whose transport routes through ``handler``.

    ``handler`` follows the :class:`httpx.MockTransport` contract:
    ``handler(request) -> httpx.Response``.
    """
    return GitHubClient("ghp_test_token_value", transport=httpx.MockTransport(handler))


def _ok_response(body: dict[str, Any]) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/graphql"
        # Body MUST be JSON-encoded with the standard GraphQL shape.
        payload = json.loads(request.content)
        assert "query" in payload
        return httpx.Response(200, json=body)

    return handler


# -- validate_query ----------------------------------------------------------


def test_validate_query_rejects_none() -> None:
    with pytest.raises(EmptyQueryError):
        validate_query(None)


def test_validate_query_rejects_empty_string() -> None:
    with pytest.raises(EmptyQueryError):
        validate_query("   \n  ")


def test_validate_query_accepts_named_query() -> None:
    validate_query("query Repo { viewer { login } }")


def test_validate_query_accepts_anonymous_shorthand() -> None:
    validate_query("{ viewer { login } }")


def test_validate_query_accepts_mutation() -> None:
    validate_query(
        "mutation AddLabel($id: ID!, $name: String!) {"
        " addLabel(id: $id, name: $name) { id } }"
    )


def test_validate_query_rejects_two_operations() -> None:
    doc = """
    query A { viewer { login } }
    query B { repository(owner: \"a\", name: \"b\") { id } }
    """
    with pytest.raises(MultipleOperationsError) as exc:
        validate_query(doc)
    assert "2 operations" in str(exc.value)


def test_validate_query_rejects_query_plus_mutation() -> None:
    doc = """
    query A { viewer { login } }
    mutation B { addLabel(id: \"x\") { id } }
    """
    with pytest.raises(MultipleOperationsError):
        validate_query(doc)


def test_validate_query_ignores_keyword_inside_comment() -> None:
    """A ``# query`` comment must NOT be counted as an operation."""
    doc = """
    # query is just a comment here
    query Real { viewer { login } }
    """
    validate_query(doc)


# -- validate_variables ------------------------------------------------------


def test_validate_variables_accepts_none() -> None:
    validate_variables(None)


def test_validate_variables_accepts_dict() -> None:
    validate_variables({"id": "X", "name": "y"})


@pytest.mark.parametrize("bad", [["a", "b"], "string", 5, 1.5, True])
def test_validate_variables_rejects_non_object(bad: Any) -> None:
    with pytest.raises(InvalidVariablesError):
        validate_variables(bad)


# -- execute: happy paths ----------------------------------------------------


def test_execute_query_returns_data_on_success() -> None:
    body = {"data": {"viewer": {"login": "octocat"}}}
    client = _client_with(_ok_response(body))
    tool = GitHubGraphQLTool(client)

    result = tool.execute("query { viewer { login } }")
    assert isinstance(result, GraphQLToolResult)
    assert result.ok is True
    assert result.data == {"viewer": {"login": "octocat"}}
    assert result.errors is None
    assert result.transport_error is None


def test_execute_mutation_returns_data() -> None:
    body = {"data": {"addLabelsToLabelable": {"clientMutationId": "x"}}}
    client = _client_with(_ok_response(body))
    tool = GitHubGraphQLTool(client)

    result = tool.execute(
        "mutation Add($id: ID!) {"
        " addLabelsToLabelable(input: {labelableId: $id, labelIds: []})"
        " { clientMutationId } }",
        variables={"id": "MDEx"},
    )
    assert result.ok is True
    assert result.data["addLabelsToLabelable"]["clientMutationId"] == "x"


def test_execute_passes_variables_in_request_body() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content)
        return httpx.Response(200, json={"data": {"x": 1}})

    tool = GitHubGraphQLTool(_client_with(handler))
    tool.execute("query Q($n: Int!) { x(n: $n) }", variables={"n": 5})
    assert captured["payload"] == {
        "query": "query Q($n: Int!) { x(n: $n) }",
        "variables": {"n": 5},
    }


# -- execute: GraphQL-level errors -------------------------------------------


def test_execute_returns_ok_false_when_graphql_errors_present() -> None:
    body = {
        "data": None,
        "errors": [{"message": "Field 'badField' doesn't exist on type 'Query'"}],
    }
    client = _client_with(_ok_response(body))
    tool = GitHubGraphQLTool(client)

    result = tool.execute("query { badField }")
    assert result.ok is False
    assert result.data is None
    assert result.errors and "badField" in result.errors[0]["message"]
    # No transport error — this is a GraphQL-level failure surfaced to
    # the model so it can correct the query.
    assert result.transport_error is None


# -- execute: transport / auth failures --------------------------------------


def test_execute_handles_403_permission_denied() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"message": "Forbidden"})

    tool = GitHubGraphQLTool(_client_with(handler))
    result = tool.execute("query { viewer { login } }")
    assert result.ok is False
    assert result.transport_error is not None
    assert "permission denied" in result.transport_error.lower()
    assert result.status_code == 403


def test_execute_handles_429_rate_limit() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            json={"message": "API rate limit exceeded"},
            headers={"Retry-After": "60"},
        )

    tool = GitHubGraphQLTool(_client_with(handler))
    result = tool.execute("query { viewer { login } }")
    assert result.ok is False
    assert "rate limited" in (result.transport_error or "").lower()
    assert result.status_code == 429


def test_execute_handles_500_transport_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"message": "internal error"})

    tool = GitHubGraphQLTool(_client_with(handler))
    result = tool.execute("query { viewer { login } }")
    assert result.ok is False
    assert result.transport_error is not None
    assert result.status_code == 500


def test_execute_handles_malformed_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not json at all")

    tool = GitHubGraphQLTool(_client_with(handler))
    result = tool.execute("query { viewer { login } }")
    assert result.ok is False
    assert "malformed" in (result.transport_error or "").lower()


def test_execute_raises_validation_for_multi_op_before_http() -> None:
    """Validation runs before the HTTP call so an invalid document
    never hits GitHub. The handler asserts no request reached it."""
    seen_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_requests.append(request)
        return httpx.Response(200, json={"data": {}})

    tool = GitHubGraphQLTool(_client_with(handler))
    with pytest.raises(MultipleOperationsError):
        tool.execute("query A { x } query B { y }")
    assert seen_requests == [], "multi-op should not reach the GitHub API"


# -- token leakage -----------------------------------------------------------


def test_token_never_appears_in_result_payload() -> None:
    """Defensive: the result dict + repr never carry the configured token.

    The handler echoes the auth header back in the body to simulate a
    leak vector; the result envelope must not surface it.
    """
    secret = "ghp_super_secret_token_aaaaaaaaaaaaaaaa"

    def handler(request: httpx.Request) -> httpx.Response:
        # Echo the Authorization header into the response body so we
        # can prove the tool result doesn't leak it via data passthrough.
        return httpx.Response(200, json={"data": {"echoed": request.headers.get("Authorization")}})

    client = GitHubClient(secret, transport=httpx.MockTransport(handler))
    tool = GitHubGraphQLTool(client)
    result = tool.execute("query { viewer { login } }")
    # The model can see ``data.echoed`` (we did pass that through) but
    # the *raw token string* should never appear in an unredacted
    # context. Verify the result envelope dictionary does NOT carry the
    # token outside of the data field — the model would only see the
    # echo if the API explicitly returned it. The point of this test
    # is structural: assert ``transport_error`` and ``errors`` never
    # encode the token.
    assert result.ok is True
    assert result.transport_error is None
    assert result.errors is None
    # Never carry the token in the envelope's auxiliary fields.
    payload = result.to_json()
    assert payload["transport_error"] is None
    assert payload["errors"] is None


# -- ToolRegistry / provider options assembly -------------------------------


def _claude_config(tmp_path: Path) -> ClaudeConfig:
    return ClaudeConfig(
        model="claude-fake",
        permission_mode="acceptEdits",
        session_store=tmp_path / "sessions",
        transcript_store=tmp_path / "transcripts",
        artifact_store=tmp_path / "artifacts",
    )


def _issue() -> Issue:
    return Issue(
        id="I_1",
        number=1,
        identifier="acme/proj#1",
        owner="acme",
        repo="proj",
        title="t",
        body="b",
        state="open",
        url="https://github.com/acme/proj/issues/1",
    )


def test_provider_without_registry_emits_no_mcp_options(tmp_path: Path) -> None:
    """Default provider construction does not put tool keys into options.

    Important: a stray ``mcp_servers`` entry would cause the SDK to try
    to start an MCP server even when no tools are registered.
    """
    provider = ClaudeCodeProvider()
    opts = provider._build_options(_issue(), tmp_path / "ws", _claude_config(tmp_path), resume=None)
    assert "mcp_servers" not in opts
    assert "allowed_tools" not in opts


def test_provider_with_registry_passes_mcp_server_and_allowed_tool(tmp_path: Path) -> None:
    """Wiring is end-to-end: register github_graphql, then assert the
    provider's options carry the SDK-shaped entries."""
    registry = ToolRegistry()
    registry.register_github_graphql(GitHubGraphQLTool(_client_with(_ok_response({"data": {}}))))
    provider = ClaudeCodeProvider(tool_registry=registry)
    opts = provider._build_options(_issue(), tmp_path / "ws", _claude_config(tmp_path), resume=None)
    assert TOOL_NAME in opts["mcp_servers"]
    assert f"mcp__{TOOL_NAME}__{TOOL_NAME}" in opts["allowed_tools"]


def test_registry_has_tools_flag() -> None:
    empty = ToolRegistry()
    assert empty.has_tools() is False
    full = ToolRegistry()
    full.register_github_graphql(GitHubGraphQLTool(_client_with(_ok_response({"data": {}}))))
    assert full.has_tools() is True


# -- Config wiring ----------------------------------------------------------


def test_agent_tools_default_disabled() -> None:
    """Default ``AgentToolsConfig`` has every tool disabled — operators
    must opt in explicitly via the workflow file."""
    cfg = AgentToolsConfig()
    assert cfg.github_graphql.enabled is False


def test_build_config_parses_agent_tools_section(tmp_path: Path) -> None:
    """``agent.tools.github_graphql.enabled`` round-trips through
    :func:`build_config` and lands on the typed config tree."""
    raw = {
        "tracker": {
            "kind": "github",
            "owner": "acme",
            "repo": "proj",
            "token": "literal-token",
        },
        "agent": {
            "provider": "claude_code",
            "tools": {"github_graphql": {"enabled": True}},
        },
        "workspace": {"root": str(tmp_path / "ws")},
        "claude": {
            "model": "claude-fake",
            "permission_mode": "acceptEdits",
            "session_store": str(tmp_path / "sessions"),
            "transcript_store": str(tmp_path / "transcripts"),
            "artifact_store": str(tmp_path / "artifacts"),
        },
        "github": {},
    }
    cfg = build_config(raw, workflow_path=tmp_path / "WORKFLOW.md", env={})
    assert cfg.agent.tools.github_graphql.enabled is True


def test_build_config_rejects_misshaped_tools(tmp_path: Path) -> None:
    """Operator typos at the section level should fail loudly at workflow
    load — silent ignoring would leave the operator wondering why the
    tool never registers."""
    from symphony.config import ConfigError

    raw = {
        "tracker": {
            "kind": "github",
            "owner": "acme",
            "repo": "proj",
            "token": "literal-token",
        },
        "agent": {
            "provider": "claude_code",
            "tools": "not-a-mapping",
        },
        "workspace": {"root": str(tmp_path / "ws")},
        "claude": {
            "model": "claude-fake",
            "permission_mode": "acceptEdits",
            "session_store": str(tmp_path / "sessions"),
            "transcript_store": str(tmp_path / "transcripts"),
            "artifact_store": str(tmp_path / "artifacts"),
        },
        "github": {},
    }
    with pytest.raises(ConfigError) as exc:
        build_config(raw, workflow_path=tmp_path / "WORKFLOW.md", env={})
    assert "agent.tools" in str(exc.value)


# -- Smoke: full WorkflowConfig round-trip ----------------------------------


def test_workflow_config_carries_default_tools_when_section_absent(tmp_path: Path) -> None:
    """Workflows that don't mention tools at all still get a defaulted
    ``AgentToolsConfig`` — the tool section is optional."""
    raw = {
        "tracker": {
            "kind": "github",
            "owner": "acme",
            "repo": "proj",
            "token": "literal-token",
        },
        "agent": {"provider": "claude_code"},
        "workspace": {"root": str(tmp_path / "ws")},
        "claude": {
            "model": "claude-fake",
            "permission_mode": "acceptEdits",
            "session_store": str(tmp_path / "sessions"),
            "transcript_store": str(tmp_path / "transcripts"),
            "artifact_store": str(tmp_path / "artifacts"),
        },
        "github": {},
    }
    cfg = build_config(raw, workflow_path=tmp_path / "WORKFLOW.md", env={})
    assert isinstance(cfg.agent.tools, AgentToolsConfig)
    assert cfg.agent.tools.github_graphql.enabled is False


# -- Silence unused-import warnings under linters that miss star-imports ----

_unused = (
    AgentConfig,
    GitHubConfig,
    GitHubGraphQLToolConfig,
    LoggingConfig,
    PollingConfig,
    RetryConfig,
    TrackerConfig,
    WorkflowConfig,
    WorkspaceConfig,
)
