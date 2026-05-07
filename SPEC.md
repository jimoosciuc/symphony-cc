# Symphony Service Specification

Status: Draft v2 for Claude Code first implementation

Purpose: Define a long-running service that orchestrates coding agents to get
project work done from issue tracker input.

This repository is named `symphony-cc`; the project, package, and CLI are still
named `symphony`.

## 1. Scope

Symphony is a daemon that reads work from an issue tracker, creates a
deterministic workspace per issue, starts a Claude Code backed coding-agent
session inside that workspace, streams normalized runtime events, and keeps
enough state and logs for operators to debug unattended runs.

The specification keeps Symphony's core orchestration responsibilities
provider-neutral. Claude Code is the first and only required provider for this
repository.

## 2. Goals

- Poll an issue tracker on a fixed cadence.
- Dispatch eligible issues with bounded concurrency.
- Create and reuse deterministic per-issue workspaces.
- Run one issue-scoped coding-agent session per active worker.
- Use Claude Code session continuity instead of isolated one-shot prompts.
- Stream provider events live to the orchestrator.
- Detect stalls, timeouts, cancellation, and process crashes.
- Retry transient failures with bounded exponential backoff.
- Reconcile active workers against tracker state changes.
- Load behavior from a repository-owned `WORKFLOW.md`.
- Persist run artifacts, logs, and provider session metadata.

## 3. Non-Goals

- Implementing a general workflow engine.
- Implementing a rich dashboard in the first milestone.
- Supporting Codex in this repository.
- Requiring a database for the first implementation.
- Moving ticket write business logic into the orchestrator.
- Reusing the old Elixir code structure.

Ticket comments, state transitions, and PR creation are normally performed by
the coding agent using tools available in its environment or by optional tools
advertised by Symphony.

## 4. System Components

1. `Workflow Loader`
   - Reads `WORKFLOW.md`.
   - Parses YAML front matter and prompt body.
   - Returns typed config plus prompt template.

2. `Config Layer`
   - Applies defaults.
   - Resolves `$ENV_VAR` values.
   - Validates settings before dispatch.

3. `Issue Tracker Adapter`
   - Fetches candidate issues.
   - Refreshes active issue states.
   - Fetches terminal issues for startup cleanup.
   - Normalizes tracker payloads into the issue model.

4. `Workspace Manager`
   - Maps each issue identifier to one workspace path.
   - Ensures workspace paths stay under `workspace.root`.
   - Runs lifecycle hooks.
   - Preserves successful workspaces unless cleanup policy says otherwise.

5. `Orchestrator`
   - Owns polling, dispatch, retries, reconciliation, and active worker state.
   - Receives normalized agent events.
   - Enforces provider-independent lifecycle rules.

6. `Agent Provider`
   - Owns provider process/session lifecycle.
   - Sends first prompt and continuation input.
   - Emits normalized events to the orchestrator.
   - Implements cancellation, timeout, cleanup, and session persistence.

7. `Logging and Artifacts`
   - Writes structured JSONL logs.
   - Stores request metadata, provider session metadata, transcripts, stderr,
     usage, terminal state, and failure details.

## 5. Issue Model

A normalized issue MUST include:

- `id`: stable tracker-internal ID.
- `identifier`: human-readable identifier, for example `ABC-123`.
- `title`.
- `description`.
- `state`.
- `url`.
- `updated_at`, if supplied by the tracker.
- `raw`, optional tracker payload for debugging.

`id` is used for tracker lookups. `identifier` is used for logs, prompt
rendering, and workspace naming.

## 6. Workflow File

The default workflow file is `WORKFLOW.md`.

It contains YAML front matter followed by a prompt template:

```markdown
---
tracker:
  kind: linear
  project_slug: symphony
agent:
  provider: claude_code
---

You are working on {{ issue.identifier }}: {{ issue.title }}.
```

Unknown top-level keys SHOULD be ignored for forward compatibility.

Required top-level config sections for v2:

- `tracker`
- `agent`
- `workspace`
- `claude`

Optional sections:

- `polling`
- `retry`
- `logging`
- `tools`

Prompt rendering MUST fail on unknown variables or unknown filters.

## 7. Configuration

### 7.1 Tracker

```yaml
tracker:
  kind: linear
  endpoint: https://api.linear.app/graphql
  api_key: $LINEAR_API_KEY
  project_slug: symphony
  active_states: ["Todo", "In Progress"]
  terminal_states: ["Closed", "Cancelled", "Canceled", "Duplicate", "Done"]
```

`tracker.kind: linear` is the only required tracker for the first
implementation.

### 7.2 Agent

```yaml
agent:
  provider: claude_code
  max_concurrency: 1
  max_turns: 3
```

`agent.provider` MUST be `claude_code` for this repository until additional
providers are explicitly added.

`max_turns` limits back-to-back continuation turns inside one worker lifetime.

### 7.3 Workspace

```yaml
workspace:
  root: .symphony/workspaces
  after_create: null
  before_run: null
  after_run: null
  before_delete: null
  hook_timeout_ms: 300000
```

Relative `workspace.root` resolves relative to the directory containing the
selected workflow file.

### 7.4 Claude

```yaml
claude:
  model: claude-sonnet-4-5
  permission_mode: acceptEdits
  session_store: .symphony/sessions
  transcript_store: .symphony/transcripts
  artifact_store: .symphony/runs
  turn_timeout_ms: 3600000
  read_timeout_ms: 30000
  stall_timeout_ms: 300000
  retry_resume_policy: resume_same_session
```

The implementation MUST document the exact Claude Code SDK version and option
names it targets. If SDK option names differ from this configuration, the
implementation MUST map Symphony config names to SDK options in one place.

`retry_resume_policy` values:

- `resume_same_session`: retry by resuming the persisted Claude session.
- `new_session_with_summary`: create a new session and include an explicit
  retry summary.
- `fail_closed`: do not retry after a provider failure.

The default SHOULD be `resume_same_session`.

## 8. Workspace Rules

Per-issue workspace path:

```text
<workspace.root>/<sanitized_issue_identifier>
```

Invariants:

- Agent execution cwd MUST equal the per-issue workspace path.
- Workspace path MUST stay inside `workspace.root`.
- Workspace directory names MUST only contain `[A-Za-z0-9._-]`; other
  characters are replaced with `_`.
- Existing workspaces are reused.
- Successful runs do not delete workspaces.

## 9. Agent Provider Contract

The provider contract is the boundary between Symphony orchestration and a
coding-agent implementation.

The orchestrator MUST NOT depend on raw Claude Code SDK event shapes. Providers
MUST translate provider-specific activity into normalized Symphony events.

### 9.1 Provider Interface

An implementation MUST provide the equivalent of:

```text
start_session(issue, workspace_path, initial_prompt, config) -> session
send_input(session, message) -> event_stream
interrupt(session) -> terminal_event
cancel(session) -> terminal_event
close(session) -> void
restore(session_record) -> session
```

`start_session` MUST create or resume an issue-scoped provider session.

`send_input` MUST support continuation input without starting an unrelated
conversation.

`restore` MUST use persisted session metadata when provider support exists. If
restore cannot be completed, the provider MUST emit a normalized failure reason
and follow the configured retry policy.

### 9.2 Session Identity

Symphony session records MUST include:

- `provider`: `claude_code`.
- `issue_id`.
- `issue_identifier`.
- `workspace_path`.
- `provider_session_id`, when available.
- `transcript_path`, when available.
- `artifact_dir`.
- `started_at`.
- `last_event_at`.
- `turn_count`.
- `attempt`.

The externally visible `session_id` SHOULD be stable and provider-neutral:

```text
<provider>:<issue_identifier>:<attempt>
```

Provider-native identifiers MUST be preserved separately as
`provider_session_id`.

### 9.3 Continuation Turns

The first turn uses the full rendered prompt.

Continuation turns SHOULD send short continuation guidance to the same provider
session rather than resending the entire prompt. The continuation guidance MUST
include current issue state and any orchestrator-observed reason for
continuing.

### 9.4 Normalized Events

Providers SHOULD emit these events:

- `session_started`
- `session_restored`
- `message_delta`
- `message_completed`
- `tool_started`
- `tool_completed`
- `permission_requested`
- `permission_resolved`
- `usage`
- `heartbeat`
- `turn_completed`
- `turn_failed`
- `turn_cancelled`
- `session_closed`
- `malformed`

Each event SHOULD include:

- `event`
- `timestamp`
- `session_id`
- `provider`
- `provider_session_id`, if available
- `issue_id`
- `issue_identifier`
- `payload`

Events MUST be streamed live enough to keep stall detection meaningful.

## 10. Claude Code Provider

Claude Code is the first provider for this repository.

### 10.1 Required Strategy

The Claude provider SHOULD use the Claude Code Agent SDK client model that
supports an ongoing session and streaming input.

The default architecture is:

```text
Symphony worker
  -> Claude provider sidecar/client
      -> one Claude Code session per issue workspace
      -> streaming input for first and continuation turns
      -> streaming output normalized into Symphony events
```

The provider MUST NOT treat isolated one-shot CLI calls as the primary
architecture. One-shot CLI invocation MAY be used only as a spike or fallback,
and that fallback MUST be documented as degraded behavior.

### 10.2 Session Persistence

For each issue, the provider MUST persist enough metadata to resume a Claude
Code session when supported:

- Claude-native session id.
- Transcript location.
- Symphony session record.
- Last successful turn metadata.
- Retry and terminal status.

If the SDK supports a configurable session store, Symphony SHOULD configure it
under `claude.session_store`.

If Claude Code stores transcripts in a provider-owned default location, Symphony
MUST record the resolved transcript location or copy a redacted artifact into
`claude.artifact_store`.

### 10.3 Permissions

Permission behavior is implementation-defined but MUST be documented.

The first implementation targets trusted local automation and MAY use a
high-trust permission mode. Permission requests MUST NOT leave a run stalled
indefinitely.

Allowed outcomes:

- auto-approve according to documented policy;
- deny and continue when the provider supports it;
- fail the turn with `permission_required`;
- surface to an operator channel, if implemented.

### 10.4 Cancellation And Interrupts

The provider MUST support best-effort cancellation.

On cancellation, timeout, process crash, or non-zero provider exit, the provider
MUST emit a terminal normalized event:

- `turn_cancelled`
- `turn_failed`
- `session_closed`

The provider MUST clean up child processes it owns. If Claude Code creates
descendant processes that cannot be controlled directly, that limitation MUST be
documented and tested with a fake provider at minimum.

### 10.5 Usage

When Claude Code exposes token or cost data, the provider SHOULD emit `usage`
events and a final usage summary.

Usage absence MUST NOT fail the run.

## 11. Orchestrator Lifecycle

On startup:

1. Load workflow config.
2. Validate tracker credentials and workspace config.
3. Create required artifact directories.
4. Clean workspaces for terminal issues when configured.
5. Start poll loop.

On each poll:

1. Fetch candidate issues.
2. Skip issues already running.
3. Respect `agent.max_concurrency`.
4. Dispatch eligible issues.
5. Reconcile active workers against tracker state.
6. Retry due failures whose backoff has expired.

Worker lifecycle:

1. Create or reuse workspace.
2. Run `after_create` for new workspaces.
3. Render first prompt from issue and workflow.
4. Run `before_run`.
5. Start or restore Claude session.
6. Stream events until turn terminal state.
7. Refresh issue state.
8. Continue on same session while active and under `agent.max_turns`.
9. Run `after_run`.
10. Preserve workspace unless terminal cleanup applies.

## 12. Retry And Recovery

Retryable failures include:

- tracker transport failure;
- provider startup failure;
- provider process crash;
- turn timeout;
- stall timeout;
- transient workspace hook failure when policy allows retry.

Non-retryable failures include:

- invalid workflow config;
- missing required credentials;
- workspace path escaping root;
- unsupported provider;
- prompt template rendering failure.

Restart recovery is tracker-driven and filesystem-driven. The first
implementation does not need to recover live in-memory workers after process
restart. It MUST reuse preserved workspaces and provider session records when
safe.

## 13. Logging And Artifacts

Structured logs SHOULD be JSONL.

Required log context:

- `timestamp`
- `level`
- `event`
- `issue_id`
- `issue_identifier`
- `session_id`
- `provider`
- `provider_session_id`, if available
- `workspace_path`
- `attempt`

Per-run artifact directory:

```text
<claude.artifact_store>/<issue_identifier>/<attempt>/
```

Recommended files:

- `request.json`
- `session.json`
- `events.jsonl`
- `provider-stderr.log`
- `provider-stdout.log`, if separate from events
- `usage.json`
- `terminal.json`

Secrets MUST be redacted from artifacts.

## 14. Optional Tool Extension

Symphony MAY expose client-side tools to the Claude session.

The first standardized optional tool is `linear_graphql`.

The tool executes one Linear GraphQL operation using Symphony's configured
tracker credentials for the active session.

The tool MUST:

- accept exactly one GraphQL operation;
- accept optional JSON object variables;
- return structured success or failure;
- avoid exposing raw credentials to the model;
- fail unsupported input without stalling the session.

## 15. HTTP Status Surface

A status surface is optional for the first implementation.

If implemented, it MUST be observational/control-plane only and MUST NOT become
required for the core scheduler.

Useful first endpoints:

- `GET /health`
- `GET /api/state`
- `POST /api/refresh`

## 16. Testing Requirements

The first implementation SHOULD include tests for:

- workflow parsing and env var resolution;
- prompt rendering failures;
- workspace path sanitization and root containment;
- fake tracker candidate/state refresh behavior;
- fake provider start/session/continuation event streams;
- orchestrator dispatch and max concurrency;
- retry backoff;
- stall timeout;
- cancellation;
- startup recovery from existing workspace/session records;
- artifact redaction.

Claude real integration tests SHOULD be skipped unless credentials and an
explicit opt-in environment variable are present.

## 17. Milestones

### Milestone 0: Design

- Adapt this spec.
- Create implementation issues.
- Review Claude provider contract before runtime code.

### Milestone 1: Core Skeleton

- Python project scaffold.
- Config and workflow parser.
- Workspace manager.
- Fake tracker.
- Fake provider.
- Orchestrator tests.

### Milestone 2: Linear And Claude Integration

- Linear tracker adapter.
- Claude Code provider.
- Session persistence.
- Artifact logging.
- Skipped real integration tests.

### Milestone 3: Local E2E

- Run one real Linear issue.
- Produce artifact bundle.
- Document observed limitations.

### Milestone 4: Hardening

- Cancellation and timeout hardening.
- Multi-turn and retry contract tests.
- Optional `linear_graphql` tool.
- Minimal status API if needed.

