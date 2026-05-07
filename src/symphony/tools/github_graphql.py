"""``github_graphql`` tool — runs one GraphQL operation under Symphony's token.

Per SPEC §18 the tool MUST:

- accept exactly one GraphQL operation per call,
- accept an optional JSON object of variables,
- never expose the raw GitHub credential to the model context,
- return a structured success/failure payload that preserves GraphQL
  errors for debugging, and
- fail without stalling the Claude session on unsupported / invalid
  input.

This module owns the validation + transport layer. The Claude provider
(``src/symphony/provider/claude_code.py``) wraps an instance of
:class:`GitHubGraphQLTool` into an SDK MCP server so Claude can invoke
``github_graphql`` by name. The wrapper marshals tool-call arguments
in/out; the validation here is the single source of truth so tests can
drive it without the SDK installed.

Result shape (consumed by both the SDK wrapper and tests):

``{
    "ok": bool,
    "data": <graphql data>     | None,
    "errors": <graphql errors> | None,
    "validation_error": <str>  | None,
    "transport_error": <str>   | None,
    "status_code": <int>       | None
}``

``ok`` is true only when the GraphQL endpoint returned a 200 with no
``errors`` array. The other fields surface why a call failed in a way
Claude can introspect without Symphony having to leak HTTP internals.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from symphony.github.client import (
    GitHubClient,
    GitHubError,
    GitHubMalformedResponse,
    GitHubMissingToken,
    GitHubPermissionDenied,
    GitHubRateLimited,
    GitHubTransportError,
)

_LOG = logging.getLogger("symphony.tools.github_graphql")

# The GraphQL endpoint on GitHub's REST host.
GRAPHQL_PATH = "/graphql"

# Tool name surfaced to Claude via the SDK MCP server. SPEC §18 fixes
# the public name; do not rename without coordinating with prompt
# rendering and the operator-facing docs.
TOOL_NAME = "github_graphql"

# Match GraphQL operation definitions at top level.
# - ``query Foo { ... }`` / ``mutation { ... }`` / ``subscription`` etc.
# - shorthand ``{ ... }`` (treated as a single anonymous query).
# We don't parse the document — we just count operation-level
# definitions because rejecting multi-op is the SPEC requirement and
# a regex is enough for the well-formed inputs we expect.
_OPERATION_KEYWORDS = ("query", "mutation", "subscription")
_OPERATION_RE = re.compile(
    r"\b(" + "|".join(_OPERATION_KEYWORDS) + r")\b\s*[A-Za-z_][A-Za-z_0-9]*\s*[({]|\b("
    + "|".join(_OPERATION_KEYWORDS) + r")\b\s*[({]",
    re.IGNORECASE,
)


# -- Errors -------------------------------------------------------------------


class GraphQLToolError(RuntimeError):
    """Base for input-validation failures specific to this tool.

    All transport / auth failures surface via the result envelope (see
    :func:`run`); this class is raised only for inputs the tool refuses
    to send to GitHub at all (multi-op, wrong types).
    """


class MultipleOperationsError(GraphQLToolError):
    """Document contains more than one operation definition."""


class InvalidVariablesError(GraphQLToolError):
    """``variables`` is not a JSON object."""


class EmptyQueryError(GraphQLToolError):
    """``query`` is missing or empty."""


# -- Result envelope ----------------------------------------------------------


@dataclass(slots=True)
class GraphQLToolResult:
    """Structured success/failure surface returned to the SDK wrapper.

    Mirrors the JSON shape documented at the top of this module so the
    wrapper can ``asdict()`` it without re-encoding.
    """

    ok: bool
    data: Any = None
    errors: Any = None
    validation_error: str | None = None
    transport_error: str | None = None
    status_code: int | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "data": self.data,
            "errors": self.errors,
            "validation_error": self.validation_error,
            "transport_error": self.transport_error,
            "status_code": self.status_code,
        }


# -- Tool ---------------------------------------------------------------------


class GitHubGraphQLTool:
    """One ``github_graphql`` invocation surface, bound to one
    :class:`GitHubClient`.

    Construction takes the *configured* GitHubClient (the same instance
    the tracker uses, or a tool-specific one — operator's call). Tests
    inject a client built with an ``httpx.MockTransport`` so no real
    network traffic happens.
    """

    name = TOOL_NAME

    def __init__(self, client: GitHubClient) -> None:
        self._client = client

    def execute(
        self,
        query: str | None,
        variables: Any | None = None,
    ) -> GraphQLToolResult:
        """Validate, dispatch, and shape the response.

        Returns a :class:`GraphQLToolResult` for transport / GraphQL
        outcomes; raises :class:`GraphQLToolError` subclasses for
        validation failures the SDK wrapper turns into a tool-error
        result without stalling the session.
        """
        validate_query(query)
        validate_variables(variables)
        body: dict[str, Any] = {"query": query}
        if variables is not None:
            body["variables"] = variables
        try:
            raw = self._client.post(GRAPHQL_PATH, json_body=body)
        except GitHubMissingToken as exc:
            return GraphQLToolResult(
                ok=False,
                transport_error=f"missing token: {exc}",
            )
        except GitHubPermissionDenied as exc:
            return GraphQLToolResult(
                ok=False,
                transport_error=f"permission denied: {exc}",
                status_code=getattr(exc, "status_code", None),
            )
        except GitHubRateLimited as exc:
            return GraphQLToolResult(
                ok=False,
                transport_error=f"rate limited: {exc}",
                status_code=getattr(exc, "status_code", None),
            )
        except GitHubTransportError as exc:
            return GraphQLToolResult(
                ok=False,
                transport_error=f"transport error: {exc}",
                status_code=getattr(exc, "status_code", None),
            )
        except GitHubMalformedResponse as exc:
            return GraphQLToolResult(
                ok=False,
                transport_error=f"malformed response: {exc}",
                status_code=getattr(exc, "status_code", None),
            )
        except GitHubError as exc:
            # Catch-all so a future GitHubError subclass doesn't bubble
            # an untyped exception into the SDK loop.
            _LOG.warning("github_graphql: unmapped GitHubError: %s", exc)
            return GraphQLToolResult(
                ok=False,
                transport_error=f"github error: {exc}",
                status_code=getattr(exc, "status_code", None),
            )

        if not isinstance(raw, dict):
            return GraphQLToolResult(
                ok=False,
                transport_error=f"unexpected GraphQL response shape: {type(raw).__name__}",
            )

        data = raw.get("data")
        errors = raw.get("errors")
        if errors:
            return GraphQLToolResult(ok=False, data=data, errors=errors)
        return GraphQLToolResult(ok=True, data=data, errors=None)


# -- Validation helpers (module-level so tests can call directly) ------------


def validate_query(query: str | None) -> None:
    """Raise :class:`GraphQLToolError` when ``query`` is missing,
    not a string, or contains more than one operation definition."""
    if query is None or not isinstance(query, str):
        raise EmptyQueryError("github_graphql: query must be a non-empty string")
    stripped = query.strip()
    if not stripped:
        raise EmptyQueryError("github_graphql: query is empty")
    op_count = _count_operations(stripped)
    if op_count > 1:
        raise MultipleOperationsError(
            f"github_graphql: document contains {op_count} operations; "
            "exactly one operation per call is required"
        )


def validate_variables(variables: Any) -> None:
    """``variables`` must be a JSON object (Python dict) or ``None``.

    GraphQL spec allows variables to be omitted; arrays / scalars are
    rejected because the GitHub API would reject them anyway and
    failing here keeps the failure mode local.
    """
    if variables is None:
        return
    if not isinstance(variables, dict):
        raise InvalidVariablesError(
            "github_graphql: variables must be a JSON object (got "
            f"{type(variables).__name__})"
        )


def _count_operations(query: str) -> int:
    """Approximate count of top-level GraphQL operation definitions.

    Strips comments first because ``# query`` shouldn't trigger
    multi-op detection. Then counts named/anonymous operations matched
    by :data:`_OPERATION_RE`. Plain ``{ ... }`` shorthand returns 1.
    """
    cleaned = re.sub(r"#[^\n]*", "", query)
    matches = _OPERATION_RE.findall(cleaned)
    if matches:
        return len(matches)
    # Shorthand: at least one ``{`` at the top means one anonymous query.
    if cleaned.lstrip().startswith("{"):
        return 1
    return 0


__all__ = [
    "EmptyQueryError",
    "GitHubGraphQLTool",
    "GraphQLToolError",
    "GraphQLToolResult",
    "InvalidVariablesError",
    "MultipleOperationsError",
    "TOOL_NAME",
    "validate_query",
    "validate_variables",
]
