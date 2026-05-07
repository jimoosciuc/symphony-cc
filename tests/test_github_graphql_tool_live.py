"""Opt-in live integration test for the ``github_graphql`` tool (#36).

Skipped by default. Enabled when ALL of:

- ``SYMPHONY_RUN_GRAPHQL_TOOL_INTEGRATION=1``
- ``GITHUB_TOKEN`` is set with at least ``public_repo`` scope.
- ``claude-agent-sdk`` is importable (always true once installed).

What this test exercises end-to-end:

1. Build a real :class:`GitHubGraphQLTool` against the real GitHub API.
2. Build the SDK MCP server config via :func:`build_sdk_mcp_server`.
3. Call the SDK-decorated handler directly (``sdk_tool.handler(args)``)
   so we don't need a full Claude session — that's the integration
   point the SDK exposes for in-process MCP tools.
4. Assert the response envelope is the expected shape AND carries
   live data (``viewer.login`` is non-empty).

Why direct-handler-invocation instead of a full Claude round-trip:
the SDK MCP server's transport is internal to the SDK; the
hand-decorated tool's ``handler`` attribute is the same async function
the SDK invokes when the model calls the tool. Driving it directly
proves the wiring is correct without paying for a real Claude
session in CI / local runs. A separate workflow-level smoke test
(under ``docs/m3-runbook.md``-style runbook) covers the
model-in-the-loop case.
"""

from __future__ import annotations

import os

import pytest

from symphony.github.client import GitHubClient
from symphony.tools.github_graphql import (
    GitHubGraphQLTool,
    build_sdk_mcp_server,
    build_sdk_tool,
)

_GATE_ENV = "SYMPHONY_RUN_GRAPHQL_TOOL_INTEGRATION"


def _gate() -> None:
    if os.environ.get(_GATE_ENV) != "1":
        pytest.skip(
            f"{_GATE_ENV}=1 not set — opt-in live test "
            "(see tests/test_github_graphql_tool_live.py docstring)"
        )
    if not os.environ.get("GITHUB_TOKEN"):
        pytest.skip("GITHUB_TOKEN not set — required for live GraphQL test")


def _real_client() -> GitHubClient:
    return GitHubClient(os.environ["GITHUB_TOKEN"])


# -- Live tests -------------------------------------------------------------


async def test_live_viewer_query_returns_login() -> None:
    """The simplest GraphQL query: ``{ viewer { login } }`` returns the
    authenticated user. Smoke test that the entire pipeline (handler →
    GitHubClient → graphql endpoint → envelope → MCP content shape) works
    end-to-end with real credentials."""
    _gate()
    tool = GitHubGraphQLTool(_real_client())
    sdk_tool = build_sdk_tool(tool)

    result = await sdk_tool.handler({"query": "query { viewer { login } }"})
    # MCP shape: ``{"content": [{"type": "text", "text": <json envelope>}]}``.
    import json

    envelope = json.loads(result["content"][0]["text"])
    assert envelope["ok"] is True, f"unexpected envelope: {envelope}"
    assert envelope["data"]["viewer"]["login"], "live viewer.login was empty"
    assert envelope["errors"] is None
    assert envelope["transport_error"] is None


async def test_live_invalid_query_returns_graphql_errors() -> None:
    """Invalid field on Query → 200 with ``errors`` array. The envelope
    surfaces ok=false + errors so the model can self-correct."""
    _gate()
    tool = GitHubGraphQLTool(_real_client())
    sdk_tool = build_sdk_tool(tool)

    result = await sdk_tool.handler({"query": "query { viewer { thisFieldDoesNotExist } }"})
    import json

    envelope = json.loads(result["content"][0]["text"])
    assert envelope["ok"] is False
    assert envelope["errors"], "expected GraphQL errors array"


async def test_live_build_sdk_mcp_server_is_runtime_compatible() -> None:
    """``build_sdk_mcp_server`` returns the SDK config dict the live
    SDK accepts — proves the production wiring (CLI → ToolRegistry →
    ClaudeCodeProvider._build_options) lands a server the SDK can
    register without errors."""
    _gate()
    cfg = build_sdk_mcp_server(GitHubGraphQLTool(_real_client()))
    assert cfg["type"] == "sdk"
    assert cfg["name"] == "github_graphql"
    # The instance is the real ``mcp.server.lowlevel.server.Server``.
    assert cfg["instance"] is not None
