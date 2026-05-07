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

The mapping is split so that **`start_session` does not stream events** — it
only stands the SDK client up and returns a `SessionRecord`. All event
emission, including the first turn, happens through `send_input`. This keeps
the provider boundary in lock-step with `SPEC.md` §10
(`start_session(...) -> SessionRecord` vs `send_input(...) -> stream[AgentEvent]`)
and makes first turns and continuation turns indistinguishable to the
orchestrator.

| Symphony provider method                                  | Claude Agent SDK behavior                                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| --------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `start_session(issue, workspace_path, config)`            | Build `ClaudeAgentOptions` (see §3) with no `resume`. Write `request.json` (see §5/§10). Instantiate `ClaudeSDKClient(options=...)`, `await client.connect()` (no prompt — keeps the streaming-input channel open). Construct a fresh `SessionRecord` with `provider_session_id = None`, `turn_count = 0`, `started_at = now`, and write it to `<session_store>/<session_id>.json`. Return the `SessionRecord`. **No SDK events are consumed and no `AgentEvent`s are emitted here.** The first prompt is *not* an argument to `start_session`. |
| `send_input(session, message)`                            | `await client.query(message, session_id="symphony")`, then async-iterate `receive_response()`. Yield normalized `AgentEvent`s for each SDK message (see §4) until the terminating `ResultMessage` produces `turn_completed` / `turn_failed`. The first call after `start_session` MUST also emit a synthesized `session_started` event before any other event of that turn (see §4 and §5.1 for `provider_session_id` capture); the first call after `restore` MUST emit `session_restored`. Subsequent calls emit neither. Generator MUST surface stalls via timeout cancellation (see §6). |
| `restore(session_record)`                                 | Build `ClaudeAgentOptions(resume=session_record.provider_session_id, cwd=session_record.workspace_path, …)`. Write `request.json` for the new attempt. `await client.connect()`. Update the on-disk `SessionRecord` (`attempt += 1`, `last_event_at = now`). Return the updated `SessionRecord`. **No SDK events are consumed and no `AgentEvent`s are emitted here.** The next `send_input` emits `session_restored` and runs the first turn. If `connect()` raises (unknown id, CLI store missing), the provider follows `claude.retry_resume_policy` — see §5. |
| `interrupt(session)`                                      | `await client.interrupt()`. The currently-running `send_input` generator drains its `receive_response()` and yields a final `turn_cancelled` event (the SDK delivers a `ResultMessage` with `subtype != "success"` after an interrupt). The provider MUST NOT close the client; `send_input` can be called again.                                                                                                                                                                |
| `cancel(session)`                                         | `await client.interrupt()` then `await client.disconnect()`. The in-flight `send_input` generator yields `turn_cancelled` then `session_closed`. The session is terminal after `cancel`.                                                                                                                                                                                                                                                                                          |
| `close(session)`                                          | `await client.disconnect()`. Emit `session_closed` (through whatever generator is open, or as a one-shot synthesized event if no `send_input` is in flight). Idempotent.                                                                                                                                                                                                                                                                                                          |

The orchestrator MUST NOT see `ClaudeSDKClient`, `AssistantMessage`, or any
other SDK type. The provider returns/yields only `SessionRecord` and
`AgentEvent` (defined in `SPEC.md` §5.4 / §5.5).

#### 2.1.1 Why `start_session` does not take the prompt

In an earlier draft `start_session(prompt, …)` consumed the first response
inline. That collapsed the §10 boundary: the first turn's events would have
escaped through `start_session`'s return value rather than the
`send_input` stream the orchestrator already owns. Splitting them means:

- The orchestrator drives every turn with the same generator (`send_input`),
  including the first one. Loop body in #7 stays trivial.
- `start_session` becomes a synchronous-shaped boundary (returns a record,
  cannot stall mid-turn). Stall/timeout logic only ever applies to
  `send_input`.
- `provider_session_id` being `None` immediately after `start_session` is an
  honest representation of what we know — Claude has not yet assigned a
  session id. It is filled in during the first `send_input`.

### 2.2 Continuation turns and provider_session_id stability

The single open `ClaudeSDKClient` is the mechanism. After `start_session`,
every turn (first and continuation alike) is a `client.query(message, …)` on
the same client, delivered to the same still-running CLI subprocess under the
same Claude session id. The provider:

1. Captures `session_id` from the first `AssistantMessage` (or
   terminating `ResultMessage`) of the first turn and writes it to
   `SessionRecord.provider_session_id` plus `session.json`. This is the
   one-and-only place `provider_session_id` is assigned for an attempt.
2. Stamps every subsequent `AgentEvent` envelope with that
   `provider_session_id`.
3. Does **not** pass `resume=` for in-attempt continuation — that would
   create a new session and break continuity. `resume=` is only used by
   `restore()` for the next attempt.

`session_id="symphony"` (the second positional arg to `client.query`) is the
SDK's per-conversation correlation key inside the streaming-input channel. It
is **not** the provider session id; the provider session id is the UUID the
CLI assigns and which arrives on the first `AssistantMessage` / `ResultMessage`.
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
turn-final `ResultMessage`) inside `send_input` and emits normalized
`AgentEvent`s. `start_session` and `restore` themselves emit no events
(see §2.1); the synthesized `session_started` / `session_restored` events
below are emitted by the **first** `send_input` call after each.

Mapping:

| Claude Agent SDK message                                            | Symphony `AgentEvent.event`                | Payload contents (under `payload`)                                                                                                                                       |
| ------------------------------------------------------------------- | ------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| (synthesized) — first event of first `send_input` after `start_session` | `session_started` (synthesized once)   | `{ "model": ..., "session_id": <captured from first AssistantMessage/ResultMessage>, "message_id": ... }`. Emitted *before* the first content event of that turn.        |
| (synthesized) — first event of first `send_input` after `restore`   | `session_restored` (synthesized once)      | `{ "model": ..., "session_id": ..., "message_id": ... }`. Emitted *before* the first content event of that turn.                                                          |
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
`event` name and `payload` contents. The `provider_session_id` field on the
envelope is `None` only for the synthesized `session_started` event itself
(since it is the event that *carries* the just-discovered id in its payload);
every subsequent event in the same attempt has it populated.

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

Lifecycle of these on-disk records (matches the §2.1 split):

1. **Before `client.connect()`** in `start_session`: `request.json` is written
   with the frozen options, `cwd`, `model`, `permission_mode`, and the issue
   identifier. This file captures provider intent, not Claude state, so it
   does not need a `provider_session_id`.
2. **After `client.connect()` succeeds and `start_session` returns**: an
   initial `session.json` is written with `provider_session_id = null`,
   `attempt`, `started_at`, and `terminal_state = null`. The orchestrator
   can already see a session record on disk before the first turn runs.
3. **During the first `send_input`**: as soon as the first
   `AssistantMessage` (or terminating `ResultMessage`) supplies the
   Claude-native session id, the provider patches `provider_session_id` and
   `last_event_at` into `session.json` and includes the same value in the
   synthesized `session_started` event payload.
4. **On every subsequent event flush**: `last_event_at`, `turn_count` (per
   `turn_completed` / `turn_failed`), and `terminal_state` (on terminal) are
   updated.

Restore (§2.1) follows the same rhythm for its own attempt: a new
`request.json` under the next attempt's artifact directory, then
`session.json` is updated in place with the bumped `attempt` and a refreshed
`last_event_at`. `provider_session_id` is already known from the prior
attempt and does not need to be rediscovered.

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
  - On the next attempt for the same `session_id`, the orchestrator calls
    `restore(session_record)` instead of `start_session`. The provider builds
    `ClaudeAgentOptions(resume=provider_session_id)` and runs `connect()`.
    Per §2.1, `restore()` itself emits no events; the next `send_input` is
    what emits the synthesized `session_restored` event.
  - The first `send_input` after restore SHOULD carry a brief continuation
    prompt constructed by the orchestrator (SPEC §12), not a re-send of the
    original first prompt.
  - If `restore()` fails (CLI subprocess errors during `connect()`, or
    `connect()` itself does not return within `claude.read_timeout_ms`), the
    provider raises a typed `ProviderRestoreError` from `restore()` itself
    rather than yielding a turn event (no generator is in flight yet). The
    orchestrator catches this and falls through to the next policy step.
    A separate failure mode — restore succeeds but the first `send_input`
    never produces a terminal `ResultMessage` — yields `turn_failed` with
    `payload.error = "restore_first_turn_failed"` from inside that
    `send_input` generator.
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
| `request.json`          | provider | Frozen options for this attempt: `cwd`, `model`, `permission_mode`, `resume` (if any), `issue_identifier`, `workspace_path`. Written **before** `client.connect()` in both `start_session` and `restore`. Never rewritten. |
| `session.json`          | provider | Latest snapshot of `SessionRecord`. First written when `start_session` returns (with `provider_session_id = null`). Patched during the first `send_input` once Claude reveals its session id, then on every event flush thereafter. See §5.1. |
| `events.jsonl`          | provider | One normalized `AgentEvent` per line. Source of truth for Symphony reconciliation. Append-only. First event of the attempt is the synthesized `session_started` (or `session_restored`) emitted by the first `send_input`. |
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

1. **`start_session` happy path** — returns a `SessionRecord` with
   `provider_session_id is None`, `attempt == 1`, `turn_count == 0`. No
   `AgentEvent` is yielded. `request.json` exists; `session.json` exists with
   `provider_session_id: null`.
2. **First `send_input` after `start_session`** — yields exactly one
   `session_started` event before any other event. The event's payload
   carries the captured `session_id`; the on-disk `session.json` is patched
   to the same value before the next event is yielded. Subsequent events of
   the turn are normal `message_delta` / `message_completed` /
   `turn_completed`. The event-envelope `provider_session_id` is `None` only
   on the `session_started` event itself, populated everywhere after.
3. **Continuation turn** — second `send_input` on the same session reuses the
   same `provider_session_id`; **no** `session_started` or `session_restored`
   event is emitted.
4. **`restore` happy path** — `ClaudeAgentOptions.resume` is set;
   `restore` returns a `SessionRecord` with bumped `attempt` and the
   pre-existing `provider_session_id`. No event is yielded by `restore`. The
   first `send_input` after restore emits `session_restored` as its first
   event.
5. **`restore` failure** — fake raises on `connect()` inside `restore`;
   provider does **not** yield any event from `restore` itself but raises a
   typed provider error (e.g. `ProviderRestoreError`) the orchestrator can
   catch. The next `send_input` would yield `turn_failed` with
   `payload.error == "restore_failed"` if the orchestrator chose to call it
   despite the raise; both code paths are covered by separate tests.
6. **Tool round-trip** — fake yields `ToolUseBlock` then `ToolResultBlock`;
   provider emits `tool_started` → `tool_completed` with matching
   `tool_use_id`.
7. **Permission stall** — fake emits `permission_requested` and then idles past
   `stall_timeout_ms`; provider emits `turn_cancelled` with
   `payload.subtype == "permission_stall"`.
8. **Stall timeout (no content)** — fake holds the iterator for longer than
   `stall_timeout_ms`; provider interrupts and emits `turn_cancelled`
   subtype `stall`.
9. **Turn timeout** — fake emits content but never terminates within
   `turn_timeout_ms`; provider emits `turn_cancelled` subtype `turn_timeout`,
   then `session_closed`.
10. **Interrupt mid-turn** — orchestrator calls `interrupt(session)`; provider
    yields `turn_cancelled`. Subsequent `send_input` works (recoverable).
11. **Cancel** — orchestrator calls `cancel(session)`; provider yields
    `turn_cancelled` then `session_closed`. Subsequent `send_input` raises.
12. **Provider crash** — fake's `receive_response` iterator raises
    `CLIConnectionError`; provider emits `turn_failed` subtype
    `provider_crash`, then `session_closed` reason `crash`.
13. **Malformed message** — fake yields an object that doesn't match any known
    SDK type; provider emits `malformed` and continues until terminal.
14. **Redaction** — fake `tool_started.payload.input` contains
    `{"token": "ghp_..."}`. The line written to `events.jsonl` does not
    contain the secret.
15. **Artifact set** — at session close, `request.json`, `session.json`,
    `events.jsonl`, `usage.json`, `terminal.json` all exist and are valid
    JSON / JSONL.
16. **Concurrent sessions** — two providers in the same process don't share
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

1. **End-to-end smoke** — `start_session` returns a `SessionRecord` against a
   tiny tmpdir workspace, then `send_input(session, "Reply with the literal
   text OK and nothing else")` is iterated. Asserts the first yielded event
   is `session_started`, that `turn_completed` arrives within 60 seconds, and
   that `session.json` on disk contains a populated `provider_session_id`
   after the first event.
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
