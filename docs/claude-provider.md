# Claude Code Provider Architecture

Status: Design v1 — implementation contract for `symphony.provider.claude_code`.

This document is the binding design for the Claude Code provider that satisfies
the boundary defined in `SPEC.md` §10 (Agent Provider Contract) and §11 (Claude
Code Provider). The orchestrator depends only on the normalized provider
interface; everything Claude-specific lives behind that boundary.

## 1. Target SDK Surface

- Package: [`claude-agent-sdk`](https://pypi.org/project/claude-agent-sdk/)
  (PyPI name).
- Pinned floor: `claude-agent-sdk >= 0.1.76, < 0.2`.
  - Implementation MUST pin a known-good version range. Bumping requires a
    follow-up issue and provider contract test re-run.
- Python: `>=3.10` (matches SDK requirement).
- Top-level imports the provider uses:
  ```python
  from claude_agent_sdk import (
      ClaudeSDKClient,
      ClaudeAgentOptions,
      AssistantMessage,
      UserMessage,
      SystemMessage,
      ResultMessage,
      TextBlock,
      ToolUseBlock,
      ToolResultBlock,
  )
  ```
- The provider MUST use the `ClaudeSDKClient` class (streaming/client mode).
  The one-shot `query()` helper is **not** the primary surface — see §9.

### Why client mode (and not one-shot `query()`)

The orchestrator needs:

1. Multiple continuation turns on the **same** Claude session within one worker
   attempt (`agent.max_turns`).
2. Mid-session **interrupt** (operator cancellation, reconciliation cancellation,
   stall recovery).
3. A stable **provider session id** to persist for restart recovery and the
   `resume_same_session` retry policy.
4. Live event delivery for stall detection.

`query()` is a stateless async iterator that spawns a fresh Claude Code CLI
subprocess per call. It cannot satisfy any of the four requirements above.
`ClaudeSDKClient` exposes `connect()`, `query(prompt, session_id)`,
`receive_response()`, `receive_messages()`, `interrupt()`, and `disconnect()`,
all of which the provider boundary maps onto directly.

## 2. Provider Session Lifecycle

```text
SymphonyWorker
  └── ClaudeCodeProvider
        └── ClaudeSDKClient (one per Symphony session)
              └── Claude Code CLI subprocess (cwd = workspace)
```

One `ClaudeSDKClient` instance is held open for the lifetime of a Symphony
session (which spans up to `agent.max_turns` continuation turns within a single
worker attempt). The CLI subprocess is torn down when the provider closes the
client.

### 2.1 Method mapping

| Symphony provider method                                  | Claude Agent SDK behavior                                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| --------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `start_session(issue, workspace_path, prompt, config)`    | Build `ClaudeAgentOptions` (see §3), instantiate `ClaudeSDKClient(options=...)`, `await client.connect()` (no prompt, keeps input channel open), then `await client.query(prompt, session_id="symphony")`. Iterate `receive_response()` to capture the first `AssistantMessage` so we can read the assigned `session_id` off it (or off the terminating `ResultMessage`). Persist that as `provider_session_id`. Emit `session_started`. Return a `SessionRecord`.                  |
| `send_input(session, message)`                            | `await client.query(message, session_id="symphony")`, then async-iterate `receive_response()`. Yield normalized `AgentEvent`s for each SDK message (see §4) until the terminating `ResultMessage` produces `turn_completed` / `turn_failed`. Generator MUST surface stalls via timeout cancellation (see §6).                                                                                                                                                                    |
| `restore(session_record)`                                 | Build `ClaudeAgentOptions(resume=session_record.provider_session_id, cwd=session_record.workspace_path, …)`. `await client.connect()`. Emit `session_restored`. If `resume` raises (unknown id, CLI store missing) the provider follows `claude.retry_resume_policy` — see §5.                                                                                                                                                                                                  |
| `interrupt(session)`                                      | `await client.interrupt()`. Emit `permission_resolved` style note? No — emit `turn_cancelled` once the current `receive_response()` iterator drains (the SDK delivers a final `ResultMessage` with `subtype != "success"` after an interrupt). The provider MUST NOT close the client; `send_input` can be called again.                                                                                                                                                          |
| `cancel(session)`                                         | `await client.interrupt()` then `await client.disconnect()`. Emit `turn_cancelled` followed by `session_closed`. The session is terminal after `cancel`.                                                                                                                                                                                                                                                                                                                          |
| `close(session)`                                          | `await client.disconnect()`. Emit `session_closed`. Idempotent.                                                                                                                                                                                                                                                                                                                                                                                                                  |

The orchestrator MUST NOT see `ClaudeSDKClient`, `AssistantMessage`, or any
other SDK type. The provider returns/yields only `SessionRecord` and
`AgentEvent` (defined in `SPEC.md` §5.4 / §5.5).

### 2.2 Continuation turns stay on the same session

The single open `ClaudeSDKClient` is the mechanism. Symphony does not pass
`resume=` for in-attempt continuation — it just calls `client.query(...)` again
on the same client, which the SDK delivers to the still-running CLI subprocess
under the same Claude session id. `provider_session_id` therefore stays stable
across all continuation turns of one attempt, which is what makes
`resume_same_session` meaningful for cross-attempt retries.

`session_id="symphony"` (the second positional arg to `client.query`) is the
SDK's per-conversation correlation key inside the streaming-input channel. It
is **not** the provider session id; the provider session id is the UUID the CLI
assigns and which arrives on the first `AssistantMessage` / `ResultMessage`.
The provider records both for observability but only the latter is used for
restore.

## 3. `ClaudeAgentOptions` Construction

Fields the provider sets, sourced from `claude:` block in `WORKFLOW.md`
(SPEC §7.5):

| Option                 | Source                              | Notes                                                                                |
| ---------------------- | ----------------------------------- | ------------------------------------------------------------------------------------ |
| `cwd`                  | `workspace.path`                    | Absolute path. SDK launches the CLI here; tools and edits scope to this dir.         |
| `model`                | `claude.model`                      | e.g. `claude-sonnet-4-5`. Provider does not default — config layer does.             |
| `permission_mode`      | `claude.permission_mode`            | See §7. Must be one of the SDK literals.                                             |
| `resume`               | `session_record.provider_session_id` | Set only on `restore()`.                                                             |
| `fork_session`         | always `False`                      | Symphony retries on the same id; forking would lose continuity.                      |
| `continue_conversation`| always `False`                      | Resolution by id is explicit; never resolve "last conversation in cwd".              |
| `system_prompt`        | not used in MVP                     | First-prompt content carries Symphony's instructions. Reserved for future tightening.|

The provider MUST NOT pass raw `WORKFLOW.md` YAML through to the SDK. Each
field is read off a typed `ClaudeConfig` after config validation.

## 4. Event Normalization

The provider iterates `receive_response()` (which terminates after the
turn-final `ResultMessage`) and emits normalized `AgentEvent`s. Mapping:

| Claude Agent SDK message                                            | Symphony `AgentEvent.event`                | Payload contents (under `payload`)                                                                                                                                       |
| ------------------------------------------------------------------- | ------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| First `AssistantMessage` of `start_session`                         | `session_started` (synthesized once)       | `{ "model": ..., "session_id": ..., "message_id": ... }` plus then a `message_delta` for the content.                                                                    |
| First `AssistantMessage` of `restore`                               | `session_restored` (synthesized once)      | `{ "model": ..., "session_id": ..., "message_id": ... }`.                                                                                                                |
| `AssistantMessage` containing `TextBlock`s                          | `message_delta` (one per block) and one `message_completed` at end of message | `{ "text": ..., "block_index": i }` for deltas; `{ "stop_reason": ..., "usage": {...} }` for completion.                                            |
| `AssistantMessage` containing `ToolUseBlock`                        | `tool_started`                              | `{ "tool_name": ..., "tool_use_id": ..., "input": {...} }`. Input MUST be redacted (see §8).                                                                              |
| `UserMessage` containing `ToolResultBlock`                          | `tool_completed`                            | `{ "tool_use_id": ..., "is_error": bool, "content": <stringified>, "tool_use_result": <opt raw> }`.                                                                       |
| `SystemMessage(subtype="permission_request" or similar)`            | `permission_requested`                     | `{ "subtype": ..., "data": {...} }`. (See §7 for handling.)                                                                                                              |
| `SystemMessage(subtype="permission_decision")`                      | `permission_resolved`                      | `{ "subtype": ..., "decision": ..., "data": {...} }`.                                                                                                                    |
| Any `SystemMessage` carrying token/usage info                       | `usage`                                    | `{ "usage": {...}, "subtype": ... }`.                                                                                                                                    |
| `ResultMessage(is_error=False)`                                     | `turn_completed`                           | `{ "duration_ms": ..., "duration_api_ms": ..., "num_turns": ..., "total_cost_usd": ..., "usage": {...}, "result": ..., "structured_output": ..., "permission_denials": ...}`.|
| `ResultMessage(is_error=True, subtype="cancelled" or interrupt-derived)` | `turn_cancelled`                      | Same fields as `turn_completed` plus `{ "subtype": ... }`.                                                                                                              |
| `ResultMessage(is_error=True)` other                                | `turn_failed`                              | Same fields plus `{ "subtype": ..., "error": ... }`.                                                                                                                      |
| `RateLimitEvent`, `StreamEvent`, `TaskStartedMessage`, `TaskProgressMessage`, `TaskNotificationMessage`, `HookEventMessage` | `heartbeat`                | `{ "kind": "<message_class>", "data": {...} }`. These are passed as `heartbeat` to satisfy stall detection without leaking SDK shapes.                                  |
| Anything `parse_message` cannot decode / unexpected SDK shape       | `malformed`                                | `{ "raw": <repr>, "reason": ... }`. Provider logs a warning; orchestrator decides whether to abort.                                                                       |
| Provider exit / `disconnect()`                                      | `session_closed`                           | `{ "reason": "normal" \| "cancel" \| "crash" \| "timeout" }`.                                                                                                            |

Every emitted `AgentEvent` carries the `SPEC.md` §5.5 envelope:
`event, timestamp, session_id, provider="claude_code", provider_session_id,
issue_identifier, attempt, payload`. The mapping table above only describes the
`event` name and `payload` contents.

The provider also emits `heartbeat` events on a `claude.read_timeout_ms` cadence
when the SDK iterator yields nothing — see §6. These are synthesized inside the
provider loop, not from the SDK.

## 5. Session Persistence and Retry

### 5.1 What gets persisted

For each Symphony session the provider writes a JSON file at:

```text
<claude.session_store>/<session_id>.json
```

Schema (matches `SessionRecord` from SPEC §5.4):

```json
{
  "session_id": "sym-...",
  "provider": "claude_code",
  "provider_session_id": "<uuid from CLI>",
  "issue_identifier": "owner/repo#123",
  "issue_number": 123,
  "workspace_path": "/abs/path/to/workspace",
  "artifact_dir": "/abs/path/to/artifacts/<owner>_<repo>_<n>/<attempt>",
  "transcript_path": "<see §5.2>",
  "attempt": 1,
  "turn_count": 0,
  "started_at": "...",
  "last_event_at": "...",
  "terminal_state": null
}
```

The record is written eagerly after `start_session` succeeds and updated after
each event flush (at minimum: `last_event_at`, `turn_count`, and on terminal,
`terminal_state`).

### 5.2 Transcript path

The Claude Code CLI writes its own JSONL transcript under
`~/.claude/projects/<encoded-cwd>/<session_id>.jsonl`. The provider:

1. Records the predicted CLI transcript path on the `SessionRecord` as
   `transcript_path` (best-effort; computed from `cwd` + `provider_session_id`).
2. Independently writes its own normalized `events.jsonl` to
   `<artifact_dir>/events.jsonl` (this is the source of truth for Symphony).
3. Optionally copies the CLI transcript into `<claude.transcript_store>/<session_id>.jsonl`
   on `close()` for offline inspection. Copy failures are logged but
   non-fatal — `events.jsonl` is the primary record.

### 5.3 Retry behavior

The orchestrator decides *whether* to retry; the provider implements *how* per
`claude.retry_resume_policy`:

- `resume_same_session` (default):
  - Provider's next `start_session` call for the same `session_id` reads the
    persisted `SessionRecord`, calls `restore()` which sets
    `ClaudeAgentOptions(resume=provider_session_id)`, and emits
    `session_restored`.
  - The first turn of the resumed session SHOULD be a brief continuation prompt
    constructed by the orchestrator (SPEC §12), not a re-send of the original
    first prompt.
  - If `restore()` fails (CLI subprocess errors, or no `AssistantMessage` /
    `ResultMessage` arrives within `claude.read_timeout_ms`), provider emits
    `turn_failed` with `payload.error = "restore_failed"` and falls through to
    the next policy step.
- `new_session_with_summary`:
  - Provider builds fresh `ClaudeAgentOptions` (no `resume`) and starts a new
    Claude session. The previous `provider_session_id` is recorded in
    `SessionRecord.previous_provider_session_ids` (a list, appended each retry)
    and passed to the orchestrator so the continuation prompt can include a
    "previous session id and summary" preamble.
- `fail_closed`:
  - Provider does not retry. `restore` is never called; the session is marked
    terminal after the first failure.

A single missing-binary failure (Claude Code CLI not on `PATH`) MUST be
non-retryable regardless of policy and surfaces as a config-style error to the
orchestrator.

## 6. Timeouts, Stalls, and Cancellation

Three timer concepts, all sourced from `claude:` config:

| Timer                | Source                          | Scope                          | Behavior on expiry                                                                                          |
| -------------------- | ------------------------------- | ------------------------------ | ----------------------------------------------------------------------------------------------------------- |
| `read_timeout_ms`    | `claude.read_timeout_ms` (30 s) | Per single `await` on the SDK message iterator within a turn. | Emit `heartbeat` event with `payload.kind = "read_idle"`. Continue waiting unless stall budget exceeded.    |
| `stall_timeout_ms`   | `claude.stall_timeout_ms` (5 m) | Per turn — wallclock since last *content-bearing* event (`message_delta`/`tool_started`/`tool_completed`).   | Provider calls `await client.interrupt()`, then waits up to `read_timeout_ms` for the SDK to deliver the post-interrupt `ResultMessage`, then emits `turn_cancelled` with `payload.subtype = "stall"`. |
| `turn_timeout_ms`    | `claude.turn_timeout_ms` (60 m) | Per turn wallclock from `client.query()` to terminal `ResultMessage`. | Same as stall path, but `payload.subtype = "turn_timeout"`. After this the session is terminal; provider closes the client. |

All timers are implemented with `asyncio.wait_for` / `asyncio.timeout`. The
provider MUST NOT use `signal.alarm` or thread-based timers.

External cancellation comes through `interrupt(session)` (recoverable, mid-turn)
or `cancel(session)` (terminal). Both call `client.interrupt()` first; `cancel`
also `disconnect()`s and emits `session_closed`. The provider MUST NOT block
indefinitely on either path — both are wrapped in
`asyncio.wait_for(..., timeout=read_timeout_ms)` and on timeout fall through to
killing the SDK subprocess via `disconnect()`.

CLI subprocess crash (SDK raises `CLIConnectionError`, `ProcessError`, or
yields no terminal `ResultMessage` and the iterator closes) maps to
`turn_failed` with `payload.subtype = "provider_crash"`, then `session_closed`
with `payload.reason = "crash"`. Retryability is up to the orchestrator's
retry layer (SPEC §16).

## 7. Permission Behavior

`claude.permission_mode` is passed straight to `ClaudeAgentOptions.permission_mode`.
Allowed values (from SDK):

```python
Literal["default", "acceptEdits", "plan", "bypassPermissions", "dontAsk", "auto"]
```

Symphony's stance:

- **MVP default**: `acceptEdits`. Symphony runs in an isolated workspace under a
  trusted local operator and we want unattended completion. `bypassPermissions`
  is **opt-in** and requires the operator to set it explicitly in `WORKFLOW.md`;
  the config validator MUST surface a warning when it is used.
- **`plan` mode is rejected** in the first implementation — it would block on
  user confirmation and Symphony has no human in the loop.
- The provider does **not** register a `can_use_tool` callback in MVP. If we
  need fine-grained control later it goes through that hook (which requires
  streaming mode, which we already use).
- If the SDK does emit a `permission_requested` SystemMessage despite the chosen
  mode (e.g., a tool out of scope of `acceptEdits`), the provider:
  1. Emits `permission_requested` (orchestrator + log visibility).
  2. Treats it as a stall: starts the `stall_timeout_ms` clock if not already
     running. If the SDK has not produced a `permission_resolved` or content
     event before the stall budget expires, the provider interrupts the turn
     with `payload.subtype = "permission_stall"`.

The provider MUST NOT auto-approve permission requests via any side channel.

## 8. Secret Redaction

The provider never logs `claude.token`-style secrets (Claude Code authenticates
via the operator's local CLI login; Symphony never holds an API key for it).
However, tool inputs/outputs may contain `GITHUB_TOKEN` or arbitrary repo
secrets. Before writing any payload to `events.jsonl`, the provider runs the
shared redactor (SPEC §17) over `payload` with at minimum:
`token`, `authorization`, `api_key`, `password`, plus any keys from
`logging.redact_keys`. Redaction is recursive over dicts/lists; values matching
common secret shapes (long base64-ish strings, `ghp_…`, `ghs_…`) are replaced
with `<redacted>`.

A unit test (see §11) MUST verify that a synthetic `GITHUB_TOKEN=ghp_xxxxxxxx`
in a fake `tool_started` payload is replaced before write.

## 9. Fallback: One-Shot CLI

The one-shot fallback is a *degraded* code path used only when:

1. The Claude Code CLI subprocess cannot be kept alive (e.g., a short-lived
   container environment that kills background processes), OR
2. The operator explicitly forces it via an undocumented escape hatch
   `claude.streaming = false` in `WORKFLOW.md` (config validator emits a
   `WARN: streaming disabled, restore and interrupt unavailable` message).

In fallback:

- Each Symphony "turn" maps to one `claude_agent_sdk.query(...)` call.
- Provider session id is derived from the first turn's `ResultMessage.session_id`
  and reused via `ClaudeAgentOptions(resume=...)` for subsequent turns. This is
  a best-effort continuity, not equivalent to streaming-input continuity.
- `interrupt()` is **unsupported**; the provider returns a `turn_cancelled`
  event only after the in-flight `query()` completes. `cancel()` may force-kill
  the SDK task but the underlying CLI subprocess may have already done work.
- `restore()` works the same way (`resume=` option), so cross-attempt retry
  semantics are preserved.

Fallback MUST NOT be the default. The first implementation may stub it as
`NotImplementedError("one-shot fallback not enabled")` until a real driver
asks for it.

## 10. Artifact Files

Provider-written files under `<claude.artifact_store>/<owner>_<repo>_<n>/<attempt>/`:

| File                    | Owner    | Purpose                                                                                                                                         |
| ----------------------- | -------- | ----------------------------------------------------------------------------------------------------------------------------------------------- |
| `request.json`          | provider | Frozen first-prompt context, options, model, permission_mode, workspace path. Written before `client.connect()`.                                |
| `session.json`          | provider | Latest snapshot of `SessionRecord`. Written after `start_session` and on every event flush.                                                     |
| `events.jsonl`          | provider | One normalized `AgentEvent` per line. Source of truth for Symphony reconciliation. Append-only.                                                 |
| `provider-stderr.log`   | provider | Captured stderr from the SDK subprocess if available. May be empty when SDK does not expose it; provider documents the limitation in `terminal.json`. |
| `usage.json`            | provider | Cumulative token/cost usage, refreshed on every `usage` event and finalized on `turn_completed` / `turn_failed`.                                |
| `terminal.json`         | provider | Final state: `terminal_state`, `terminal_reason`, `last_event_at`, `provider_session_id`, fallback_used (bool), errors. Written exactly once on session close. |
| `transcript.jsonl`      | provider | Optional copy of the CLI's own transcript file. Best-effort; absence is not an error.                                                           |

`github.json` is owned by the GitHub tracker, not the provider.

## 11. Test Plan

### 11.1 Fake-tier (always run)

In-process tests against a `FakeClaudeSDKClient` that the provider talks to
through dependency injection. The fake implements the same async surface
(`connect`, `query`, `receive_response`, `interrupt`, `disconnect`) and yields
scripted `AssistantMessage` / `ResultMessage` / `SystemMessage` instances
constructed from the real SDK dataclasses (no mocking of dataclass shape).

Required cases:

1. **`start_session` happy path** — yields `session_started`, one or more
   `message_delta`, `message_completed`, `turn_completed`. `SessionRecord`
   contains `provider_session_id`.
2. **Continuation turn** — second `send_input` on the same session reuses the
   same `provider_session_id`, no `session_restored` event emitted.
3. **`restore` happy path** — `ClaudeAgentOptions.resume` is set,
   `session_restored` emitted, then a normal turn proceeds.
4. **`restore` failure** — fake raises on `connect()`; provider emits
   `turn_failed` with `payload.error == "restore_failed"`.
5. **Tool round-trip** — fake yields `ToolUseBlock` then `ToolResultBlock`;
   provider emits `tool_started` → `tool_completed` with matching
   `tool_use_id`.
6. **Permission stall** — fake emits `permission_requested` and then idles past
   `stall_timeout_ms`; provider emits `turn_cancelled` with
   `payload.subtype == "permission_stall"`.
7. **Stall timeout (no content)** — fake holds the iterator for longer than
   `stall_timeout_ms`; provider interrupts and emits `turn_cancelled`
   subtype `stall`.
8. **Turn timeout** — fake emits content but never terminates within
   `turn_timeout_ms`; provider emits `turn_cancelled` subtype `turn_timeout`,
   then `session_closed`.
9. **Interrupt mid-turn** — orchestrator calls `interrupt(session)`; provider
   yields `turn_cancelled`. Subsequent `send_input` works (recoverable).
10. **Cancel** — orchestrator calls `cancel(session)`; provider yields
    `turn_cancelled` then `session_closed`. Subsequent `send_input` raises.
11. **Provider crash** — fake's `receive_response` iterator raises
    `CLIConnectionError`; provider emits `turn_failed` subtype
    `provider_crash`, then `session_closed` reason `crash`.
12. **Malformed message** — fake yields an object that doesn't match any known
    SDK type; provider emits `malformed` and continues until terminal.
13. **Redaction** — fake `tool_started.payload.input` contains
    `{"token": "ghp_..."}`. The line written to `events.jsonl` does not
    contain the secret.
14. **Artifact set** — at session close, `request.json`, `session.json`,
    `events.jsonl`, `usage.json`, `terminal.json` all exist and are valid
    JSON / JSONL.
15. **Concurrent sessions** — two providers in the same process don't share
    state; `events.jsonl` paths and `provider_session_id`s are independent.

These tests use `pytest` + `pytest-asyncio`. They MUST run without network
access and without the `claude` CLI installed.

### 11.2 Live-tier (opt-in)

Skipped by default. Enabled only when **all** of:

- `SYMPHONY_RUN_CLAUDE_INTEGRATION=1`
- The `claude` CLI is on `PATH` and authenticated (the test asserts this and
  skips with a clear message if not).
- `pytest -m claude_live` is selected.

Required cases:

1. **End-to-end smoke** — `start_session` + one trivial prompt
   (`"Reply with the literal text OK and nothing else"`) against a tiny
   tmpdir workspace. Asserts a `turn_completed` event arrives within
   60 seconds.
2. **Continuation** — same session, second prompt
   (`"Now reply with the literal text DONE"`). Asserts the second
   `turn_completed` shares `provider_session_id` with the first.
3. **Restore** — close the client, instantiate a new provider, call
   `restore()` with the persisted record, send a third prompt, assert success
   and same `provider_session_id`.
4. **Interrupt** — issue a long-running prompt (`"Count slowly to 1000"`),
   call `interrupt()` after the first `message_delta`, assert
   `turn_cancelled` arrives within 10 seconds.

Live tests use a temporary `claude.session_store` and `claude.artifact_store`
under `tmp_path`. They MUST tear down the `ClaudeSDKClient` even on failure.

### 11.3 Contract tests (M4)

`tests/test_provider_contract.py` runs the same scripted scenarios against
**both** the fake provider and the real `ClaudeCodeProvider` (with a fake
SDK client) to guarantee behavior parity. This is what M4 #12 hardens.

## 12. Open Questions

These do not block M0 closure but should be tracked as follow-ups:

- Whether to expose Symphony's `github_graphql` tool (M4 #13) via the SDK's
  `mcp_servers` option vs. a simpler intercept in `can_use_tool`. Decision
  deferred to M4 design.
- Whether `bypassPermissions` should be allowed at all for the first public
  release, or only behind a `--unsafe` CLI flag. Owner: operator UX.
- Whether to mirror the CLI transcript copy into a content-addressed store
  for cross-attempt diffing. Probably not for MVP.
