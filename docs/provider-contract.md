# Provider contract

**Audience:** anyone implementing a new `AgentProviderProtocol` for Symphony, or
extending the existing Claude Code provider in a way that touches the
event surface or the session lifecycle.

**Source of truth:** `SPEC.md` §10 (provider boundary), §17 (event /
artifact schema), §11 (timeouts), `docs/claude-provider.md` §2.1
(no-stream-from-start_session split). This document summarizes those
contracts in test-runnable form. The tests live at
`tests/test_provider_contract.py` and the fixture event scripts at
`tests/fixtures/provider_events/`.

## Provider lifecycle

Symphony's orchestrator drives every provider through the same nine-step
sequence:

1. `start_session(issue, workspace_path, config)` — connect, return a
   `SessionRecord`. **No events.**
2. `send_input(record, prompt)` — async generator. First call after
   `start_session` MUST emit `session_started` as its first event;
   stream eventually ends with one of `turn_completed` /
   `turn_failed` / `turn_cancelled`.
3. The orchestrator MAY call `send_input(record, ...)` again (multi-turn
   continuation) up to `agent.max_turns`. Continuation calls MUST NOT
   re-emit `session_started`.
4. `interrupt(record)` — best-effort cancel of the in-flight turn.
   Returns one `AgentEvent` envelope (typically `turn_cancelled`).
5. `cancel(record)` — interrupt + disconnect. Returns one
   `AgentEvent` envelope (e.g. `session_closed`).
6. `close(record)` — idempotent disconnect. Safe to call from `finally`.
7. `restore(record)` — reconnect to a prior session by
   `provider_session_id`. Bumps `attempt`. First `send_input` after
   restore MUST emit `session_restored` (not `session_started`).
8. `restore` MUST raise `ProviderRestoreError` when
   `provider_session_id` is empty / missing.
9. After `close` (or after `cancel`), `send_input` MUST raise
   `ProviderError`. The orchestrator depends on the lifecycle violation
   being loud, not silent.

## Event envelope

Every emitted `AgentEvent` carries the SPEC §17 envelope:

| field | type | notes |
|---|---|---|
| `event` | `str` | one of the well-known names (see below) |
| `timestamp` | `datetime` | UTC, set at emit time |
| `session_id` | `str` | matches the `SessionRecord` |
| `provider` | `str` | matches `provider.name` |
| `issue_identifier` | `str` | `<owner>/<repo>#<n>` |
| `attempt` | `int` | 1-based; `restore` bumps it |
| `payload` | `dict` | event-specific |
| `provider_session_id` | `str \| None` | populated after first send_input |

The fields are checked by `tests/_provider_contract.py:assert_event_envelope_shape`
on every event yielded during the contract sweep.

## Event names

The orchestrator and artifact writer match on these names. New
providers MUST normalize to this set (or to `heartbeat` /
`malformed`):

- **lifecycle**: `session_started`, `session_restored`, `session_closed`
- **content**: `message_delta`, `message_completed`,
  `tool_started`, `tool_completed`
- **system**: `permission_requested`, `permission_resolved`, `usage`
- **terminal**: `turn_completed`, `turn_failed`, `turn_cancelled`
- **other**: `heartbeat` (any non-content SDK signal — rate limits,
  task progress, hooks), `malformed` (unknown SDK message class)

## Session record

`SessionRecord` (SPEC §5.4) is mutable on purpose. Providers populate
`provider_session_id` after the SDK reveals it (typically on the first
`AssistantMessage`); update `turn_count` after every terminal turn
event; carry `previous_provider_session_ids` across `restore` calls so
the continuation prompt can reference the prior conversation.

Required fields are checked by
`tests/_provider_contract.py:assert_session_record_shape`.

## Fixture event scripts

Reusable scenario files under `tests/fixtures/provider_events/`. Each
file is JSONL where every line is `{"event": <name>, "payload": <dict>}`
in Symphony's normalized AgentEvent shape. A special `__raise__` line
aborts the stream by raising the named provider exception (used to
script crash-mid-stream behavior).

Required fixtures:

| name | what it covers |
|---|---|
| `normal_completion` | single-turn happy path |
| `tool_round_trip` | tool_started → tool_completed |
| `permission_request` | permission_requested → permission_resolved |
| `usage_heartbeat` | system message with usage data |
| `rate_limit` | rate-limit heartbeat carries `kind="rate_limit"` |
| `malformed` | unknown SDK message class → `malformed` event |
| `cancelled` | partial output + `turn_cancelled` |
| `crash_after_partial` | partial output + `__raise__` (retryable) |
| `turn_failed` | terminal `turn_failed` with non-retryable subtype |

## Adding a new provider

1. Implement the methods listed in **Provider lifecycle** above.
2. Set `provider.name` to a stable string.
3. Wire your factory into `tests/test_provider_contract.py:PROVIDER_BUILDERS`:
   ```python
   PROVIDER_BUILDERS = [
       ("fake", make_fake_provider_for),
       ("claude_code", make_claude_provider_for),
       ("your_provider", make_your_provider_for),
   ]
   ```
4. Run the contract suite:
   ```bash
   pytest tests/test_provider_contract.py -q
   ```
   The full sweep (~24 tests × N providers + 11 fixture-driven scenarios
   × N providers) lights up everything that needs to hold for the
   orchestrator to work.

## What contract tests do NOT cover

- Provider-specific error mapping (e.g. `ClaudeCodeProvider` mapping
  `CLIConnectionError` → `ProviderRetryableError` lives in
  `tests/test_claude_provider.py`).
- Per-SDK-message normalization details — the contract tests only
  assert that the *normalized* event stream satisfies the envelope and
  terminal-event constraints, not that every SDK message is mapped to
  the exact right event name.
- Orchestrator-level behavior (timeouts, retries, blocked-label
  routing) — those live in `tests/test_orchestrator.py`,
  `tests/test_timeouts.py`, `tests/test_recovery.py`.
- Live SDK round-trips — gated behind `SYMPHONY_RUN_*_INTEGRATION=1`
  in `tests/test_claude_provider_live.py`.
