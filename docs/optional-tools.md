# Optional client-side tools

**Audience:** operators enabling Symphony's optional tools in
`WORKFLOW.md`, and contributors adding new tools under
`src/symphony/tools/`.

**Source of truth:** `SPEC.md` §18, `docs/claude-provider.md` §12.

## What an "optional tool" is

A piece of code Symphony exposes to the Claude session as an MCP tool.
The session calls the tool by name; the handler runs in Symphony's
process, against Symphony's credentials, and returns a structured
result the model consumes. The raw credential never enters the model
context.

Tools are off by default. Each tool is gated by its own
`agent.tools.<name>.enabled` knob in the workflow file.

## `github_graphql`

Runs one GitHub GraphQL operation under the same token
(`tracker.token`) Symphony already uses for issue management.

### Enabling

```yaml
agent:
  provider: claude_code
  tools:
    github_graphql:
      enabled: true
```

### Tool surface (as the model sees it)

- Tool name: `github_graphql`
- Inputs: `query: string` (required), `variables: object | null`
  (optional)
- Result envelope:
  ```json
  {
    "ok": true | false,
    "data": <graphql data> | null,
    "errors": <graphql errors> | null,
    "validation_error": <string> | null,
    "transport_error": <string> | null,
    "status_code": <int> | null
  }
  ```

`ok` is true only when the endpoint returned a 200 with no `errors`
array. The auxiliary fields surface why a call failed:

- `validation_error` — Symphony refused the input before any HTTP
  call (multi-op document, non-object variables, empty query).
- `transport_error` — auth failed, rate limited, transport blew up,
  or the response wasn't JSON. `status_code` carries the HTTP code
  when applicable.
- GraphQL-level errors (bad field, missing variable) come back in
  `errors` with `ok=false`, leaving the model free to correct and
  retry.

### Constraints (SPEC §18)

- **Exactly one operation per call.** Documents with two `query` /
  `mutation` / `subscription` definitions are rejected before any
  HTTP call. Use a single named operation when the model needs to
  pick.
- **No raw credential exposure.** The token lives only inside
  Symphony's `GitHubClient`; it does not enter the prompt, event
  payloads, or artifacts. The `<redacted>` machinery in
  `symphony.artifacts.redact` covers the defensive case where a token
  shape leaks via a tool input.
- **Fail-soft.** Validation failures and transport failures both
  return a structured envelope rather than raising into the SDK loop
  — Claude can react without stalling the session.

### What the tool DOES NOT do

- It does not paginate. Multi-page queries should use the GraphQL
  `pageInfo` cursor pattern explicitly.
- It does not enforce a per-call cost budget — operators should rely
  on GitHub's own rate-limiting and the SPEC §16 retry/backoff if a
  burst trips the limiter.
- It does not auto-retry transient failures. The SDK retry loop is
  per-turn, not per-tool-call.

### Limitations (current)

- **SDK MCP wrapper is not yet wired end-to-end.** The handler logic
  (`GitHubGraphQLTool.execute`) is fully unit-tested, and Symphony's
  `ToolRegistry` puts a `_GitHubGraphQLMcpEntry` into
  `ClaudeAgentOptions.mcp_servers` when the tool is enabled. The
  SDK-side adapter that translates an MCP tool-call from the model
  into `tool.execute(query, variables)` is the remaining piece.
  Setting `enabled: true` today advertises the tool name to Claude
  but tool calls will not execute until #36 ships. Track:
  [#36](https://github.com/jimoosciuc/symphony-cc/issues/36).
- No live integration test for this tool yet — coverage stops at the
  unit-tested handler. The end-to-end smoke test against a real
  Claude session is part of #36.

## Adding a new tool

1. Create `src/symphony/tools/<name>.py` with a pure-Python handler
   class. Take only the things the handler needs (HTTP client, file
   path, etc.) — no `WorkflowConfig`.
2. Add a config dataclass + a builder branch under
   `src/symphony/config.py:_build_agent_tools`. Keep `enabled` defaulting
   to false.
3. Wire into `ToolRegistry` (in
   `src/symphony/provider/claude_code.py`) with a `register_<name>`
   method that records the SDK MCP server entry + the
   `mcp__<server>__<tool>` allowed-tools name.
4. Update `src/symphony/cli.py:_build_tool_registry` to honor the new
   knob.
5. Add tests under `tests/test_<name>_tool.py` covering: validation,
   transport / auth failures, and the provider's options assembly
   when the registry has the tool registered.
6. Update this document with the new tool's section.

The contract matters: tools must fail-soft (return a structured
envelope) rather than raising into the SDK event loop, must never
expose raw credentials to the model, and must be opt-in. Anything
that breaks those is a SPEC §18 violation.
