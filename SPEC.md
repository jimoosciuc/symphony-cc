# Symphony Service Specification

Status: Draft v2, Claude Code first and GitHub first

Purpose: Define a long-running service that uses Claude Code to complete work
from GitHub issues and return changes through GitHub pull requests.

This repository is named `symphony-cc`; the product, package, and CLI remain
named `symphony`.

## 1. Problem Statement

Symphony is a daemon that continuously reads eligible GitHub issues, creates a
deterministic workspace per issue, starts an issue-scoped Claude Code session,
streams normalized runtime events, and leaves enough GitHub and filesystem
evidence for operators to review unattended work.

The service solves these operational problems:

- It turns issue execution into a repeatable daemon workflow.
- It isolates agent execution in per-issue workspaces.
- It keeps workflow policy in a versioned `WORKFLOW.md`.
- It makes Claude Code session continuity explicit and inspectable.
- It uses GitHub issues, pull requests, labels, comments, and optionally GitHub
  Projects as the work coordination surface.
- It provides enough logs and artifacts to debug multiple concurrent runs.

Important boundary:

- Symphony is a scheduler, runner, GitHub reader, and GitHub work coordinator.
- Code changes, validation, commits, and PR creation may be performed by the
  Claude Code session, by Symphony helper code, or by a documented hybrid.
- Symphony MUST preserve enough state to avoid duplicate work and to resume
  useful operation after restart.

## 2. Goals

- Poll GitHub issues on a fixed cadence.
- Dispatch eligible issues with bounded concurrency.
- Claim issues with labels/comments so humans and future runs can see ownership.
- Create deterministic per-issue workspaces and preserve them across attempts.
- Start one Claude Code session per active issue worker.
- Use Claude Code session continuity instead of isolated one-shot prompts.
- Stream provider events live to orchestrator state and JSONL logs.
- Detect stalls, timeouts, cancellation, and provider crashes.
- Retry transient failures with bounded backoff.
- Reconcile active workers against GitHub issue/PR state changes.
- Produce or update a GitHub pull request for completed code work.
- Support tracker/filesystem/session-record restart recovery without requiring a
  database in the first implementation.
- Provide a focused test matrix that lets future agents implement issues
  mechanically.

## 3. Non-Goals

- Supporting Linear in the first implementation.
- Supporting Codex in this repository.
- Implementing a general workflow engine.
- Requiring GitHub Projects for the first implementation.
- Building a rich dashboard before the core daemon works.
- Reusing the old Elixir implementation structure.
- Providing strong sandbox guarantees beyond the documented Claude Code and host
  OS controls.

## 4. Architecture

### 4.1 Components

1. `Workflow Loader`
   - Reads `WORKFLOW.md`.
   - Parses YAML front matter and prompt body.
   - Returns typed config and prompt template.

2. `Config Layer`
   - Applies defaults.
   - Resolves `$ENV_VAR` values.
   - Normalizes paths.
   - Validates settings before dispatch.

3. `GitHub Tracker Adapter`
   - Fetches candidate issues.
   - Fetches current issue states for reconciliation.
   - Claims and releases issues through labels and comments.
   - Discovers linked PRs.
   - Optionally reads GitHub Project fields for eligibility and status.

4. `Workspace Manager`
   - Maps each issue to a workspace path.
   - Ensures workspace paths stay under `workspace.root`.
   - Populates or reuses repositories.
   - Runs lifecycle hooks.
   - Cleans terminal workspaces when configured.

5. `Orchestrator`
   - Owns poll ticks, dispatch, active workers, retries, and reconciliation.
   - Receives normalized provider events.
   - Maintains in-memory runtime state.
   - Persists per-run artifacts.

6. `Agent Provider`
   - Owns provider process/session lifecycle.
   - Sends first and continuation inputs.
   - Emits normalized events.
   - Implements interruption, cancellation, cleanup, session persistence, and
     provider-specific error mapping.

7. `Claude Code Provider`
   - Implements the provider boundary with Claude Code.
   - Uses an ongoing Claude Code session with streaming input.
   - Persists provider session identity and transcript metadata.

8. `PR Coordinator`
   - Defines branch naming, PR discovery, PR creation/update policy, and review
     handoff.
   - May be implemented as helper code, prompt instructions, or an advertised
     tool, but behavior MUST be explicit.

9. `Logging and Artifacts`
   - Emits structured logs.
   - Stores request, session, event, usage, terminal, and GitHub metadata.

### 4.2 Layers

Symphony is easiest to implement when kept in these layers:

1. Policy: `WORKFLOW.md` prompt body and workflow settings.
2. Configuration: typed config and validation.
3. Coordination: polling, dispatch, retries, reconciliation.
4. Execution: workspace and provider session.
5. Integration: GitHub API and optional project metadata.
6. Observability: logs, artifacts, and optional status API.

## 5. Data Models

### 5.1 Issue

A normalized issue MUST include:

- `id`: GitHub node ID or stable issue ID.
- `number`: repository issue number.
- `identifier`: stable human-readable identifier, for example `owner/repo#123`.
- `owner`.
- `repo`.
- `title`.
- `body`.
- `state`: `open` or `closed`.
- `url`.
- `labels`.
- `assignees`.
- `updated_at`.
- `created_at`.
- `raw`: optional raw payload for debugging.

`identifier` is used for logs, prompt rendering, workspace naming, and
operator-facing status.

### 5.2 Pull Request

A normalized pull request SHOULD include:

- `id`.
- `number`.
- `owner`.
- `repo`.
- `title`.
- `url`.
- `state`.
- `head_ref`.
- `base_ref`.
- `is_draft`.
- `mergeable_state`, if available.
- `linked_issue_identifier`, if known.

### 5.3 Workspace

Workspace record:

- `issue_identifier`.
- `workspace_key`.
- `path`.
- `repo_path`.
- `created_at`.
- `reused`.

### 5.4 Session Record

Session record:

- `session_id`.
- `provider`: `claude_code`.
- `provider_session_id`, if available.
- `issue_identifier`.
- `issue_number`.
- `workspace_path`.
- `artifact_dir`.
- `transcript_path`, if available.
- `attempt`.
- `turn_count`.
- `started_at`.
- `last_event_at`.
- `terminal_state`, if ended.

### 5.5 Runtime Event

Runtime event:

- `event`.
- `timestamp`.
- `session_id`.
- `provider`.
- `provider_session_id`, if available.
- `issue_identifier`.
- `attempt`.
- `payload`.

## 6. Workflow File

The default workflow file is `WORKFLOW.md`.

It contains YAML front matter followed by a prompt template:

```markdown
---
tracker:
  kind: github
  owner: jimoosciuc
  repo: symphony-cc
agent:
  provider: claude_code
---

You are working on {{ issue.identifier }}: {{ issue.title }}.
```

Required top-level sections:

- `tracker`
- `agent`
- `workspace`
- `claude`
- `github`

Optional sections:

- `polling`
- `retry`
- `logging`
- `tools`
- `status`

Unknown top-level keys SHOULD be ignored for forward compatibility.

Prompt rendering MUST fail on unknown variables or unknown filters.

## 7. Configuration

### 7.1 Tracker

```yaml
tracker:
  kind: github
  owner: jimoosciuc
  repo: symphony-cc
  token: $GITHUB_TOKEN
  include_labels: ["symphony-ready"]
  exclude_labels: ["symphony-running", "symphony-blocked"]
  terminal_labels: ["symphony-done", "symphony-wontfix"]
  active_states: ["open"]
```

`tracker.kind` MUST be `github` in the first implementation.

Candidate issues:

- MUST be open.
- MUST match `include_labels` when configured.
- MUST NOT have any `exclude_labels`.
- MUST NOT already have an active Symphony claim by another live run.
- MAY be filtered by assignee, milestone, repository, or project fields if
  configured.

### 7.2 GitHub Coordination

```yaml
github:
  claim_label: symphony-running
  ready_label: symphony-ready
  blocked_label: symphony-blocked
  done_label: symphony-done
  branch_prefix: symphony
  base_branch: main
  draft_pr: true
  claim_comment: true
  pr_link_comment: true
  close_issue_on_done: false
  project:
    enabled: false
    owner: null
    number: null
    status_field: Status
    ready_values: ["Ready"]
    running_value: "In Progress"
    review_value: "Review"
```

GitHub Projects are OPTIONAL. The first implementation SHOULD work with issues,
labels, comments, and PRs only.

### 7.3 Agent

```yaml
agent:
  provider: claude_code
  max_concurrency: 1
  max_turns: 3
```

`agent.provider` MUST be `claude_code`.

`max_turns` limits back-to-back continuation turns inside one worker lifetime.

### 7.4 Workspace

```yaml
workspace:
  root: .symphony/workspaces
  populate: git
  remote: origin
  after_create: null
  before_run: null
  after_run: null
  before_delete: null
  hook_timeout_ms: 300000
```

Relative paths resolve relative to the workflow file directory.

`populate: git` means the implementation prepares a git checkout suitable for
the issue. Exact clone/fetch behavior is implementation-defined but MUST be
documented.

### 7.5 Claude

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
names it targets.

`retry_resume_policy` values:

- `resume_same_session`: retry by resuming the persisted Claude session.
- `new_session_with_summary`: create a new session and include a retry summary.
- `fail_closed`: do not retry after provider failure.

### 7.6 Polling And Retry

```yaml
polling:
  interval_ms: 60000
  reconcile_interval_ms: 30000

retry:
  max_attempts: 3
  initial_backoff_ms: 60000
  max_backoff_ms: 900000
  multiplier: 2.0
```

### 7.7 Logging

```yaml
logging:
  level: info
  jsonl_path: .symphony/logs/symphony.jsonl
  redact_keys: ["token", "authorization", "api_key", "password"]
```

## 8. Workspace Contract

Per-issue workspace path:

```text
<workspace.root>/<sanitized_owner>_<sanitized_repo>_<issue_number>
```

Invariants:

- Agent execution cwd MUST equal the per-issue workspace path or configured
  repository subdirectory inside it.
- Workspace path MUST stay inside `workspace.root`.
- Workspace names MUST only contain `[A-Za-z0-9._-]`; other characters are
  replaced with `_`.
- Existing workspaces are reused.
- Successful workspaces are preserved by default.

Workspace preparation:

- Ensure directory exists.
- Populate or update repository checkout.
- Create or update the issue branch.
- Run `after_create` only for new workspace directories.
- Run `before_run` before provider start.
- Run `after_run` after provider terminal state.
- Run `before_delete` before cleanup.

Branch naming SHOULD default to:

```text
<github.branch_prefix>/<owner>-<repo>-<issue_number>
```

## 9. GitHub Tracker Contract

### 9.1 Required Operations

The tracker adapter MUST support:

1. `fetch_candidate_issues(config) -> list[Issue]`
2. `fetch_issues_by_numbers(numbers) -> list[Issue]`
3. `claim_issue(issue, run_metadata) -> claim_result`
4. `release_issue(issue, reason) -> release_result`
5. `mark_issue_blocked(issue, reason) -> result`
6. `find_linked_pull_requests(issue) -> list[PullRequest]`
7. `create_or_update_progress_comment(issue, body) -> result`
8. `create_or_update_pr_link_comment(issue, pr) -> result`

The PR coordinator or Claude session MUST support:

1. `ensure_branch(issue, workspace) -> branch`
2. `ensure_pull_request(issue, branch, summary) -> PullRequest`

### 9.2 Claim Semantics

Before dispatching a worker, Symphony SHOULD claim the issue:

- Add `github.claim_label`.
- Optionally remove `github.ready_label`.
- Optionally write a claim comment with run ID, workspace, and timestamp.
- Optionally update project status to running.

If claim fails because the issue changed or another run owns it, dispatch MUST
skip the issue.

### 9.3 Release And Completion

On failure:

- Remove `claim_label` when the run has ended.
- Add `blocked_label` for non-retryable failures when configured.
- Write a failure comment or artifact link when configured.

On successful PR creation:

- Remove `claim_label`.
- Add or preserve review/done labels according to workflow config.
- Comment with PR URL when `pr_link_comment` is true.
- Optionally update project status to review.
- Do not close the issue unless `close_issue_on_done` is true.

### 9.4 GitHub API Requirements

The implementation MAY use GitHub REST, GraphQL, or `gh` CLI internally, but the
adapter boundary MUST expose normalized models and errors.

Required auth:

- `GITHUB_TOKEN` or configured token with access to issues, contents, pull
  requests, and project metadata when project integration is enabled.

Errors MUST distinguish:

- missing token;
- permission denied;
- repository not found;
- rate limit;
- transport failure;
- malformed response;
- claim conflict.

## 10. Agent Provider Contract

The orchestrator MUST NOT depend on raw Claude Code SDK event shapes.

Provider interface:

```text
start_session(issue, workspace_path, config) -> SessionRecord
send_input(session, message) -> stream[AgentEvent]
interrupt(session) -> AgentEvent
cancel(session) -> AgentEvent
close(session) -> None
restore(session_record) -> SessionRecord
```

`start_session` MUST create an issue-scoped provider session and return a
`SessionRecord`. It MUST NOT take the first prompt and MUST NOT stream
agent events. The orchestrator drives the first turn — and every
continuation turn — through `send_input`.

`restore` MUST use persisted session metadata when provider support exists
to resume the previous provider session. Like `start_session`, it returns a
`SessionRecord` and does not stream events; the next `send_input` runs the
first turn of the resumed session.

`send_input` MUST support continuation input without starting an unrelated
conversation. The first `send_input` after `start_session` MUST emit a
`session_started` event before any other event of that turn; the first
`send_input` after `restore` MUST emit `session_restored`.

If `restore` cannot complete (provider startup fails before any turn), the
provider MUST raise a typed restore-startup failure that the orchestrator can
catch and route to `retry_resume_policy`. Failures observed during a turn
(after `send_input` is called) MUST be reported as a normalized terminal
event on that turn's stream.

Normalized event names:

- `session_started`
- `session_restored`
- `heartbeat`
- `message_delta`
- `message_completed`
- `tool_started`
- `tool_completed`
- `permission_requested`
- `permission_resolved`
- `usage`
- `turn_completed`
- `turn_failed`
- `turn_cancelled`
- `session_closed`
- `malformed`

Events MUST be streamed live enough for stall detection.

## 11. Claude Code Provider

The Claude provider SHOULD use the Claude Code Agent SDK client mode that
supports ongoing sessions and streaming input.

Default architecture:

```text
Symphony worker
  -> ClaudeCodeProvider
      -> one Claude Code session per issue workspace
      -> streaming input for first and continuation turns
      -> normalized event stream
      -> persisted provider session metadata
```

The provider MUST NOT treat isolated one-shot CLI calls as the primary
architecture. One-shot CLI MAY be a degraded fallback only when documented.

Required behavior:

- Start Claude in the issue workspace.
- Send the rendered first prompt.
- Send continuation guidance on the same session.
- Persist provider-native session identity when available.
- Record transcript location or copy transcript artifacts.
- Emit usage when available.
- Handle permission requests according to documented policy.
- Support best-effort interrupt/cancel.
- Emit terminal events for timeout, cancellation, crash, and provider failure.

Permission behavior is implementation-defined but MUST NOT stall indefinitely.

## 12. Prompt Contract

The first prompt SHOULD include:

- issue identifier, title, body, labels, and URL;
- repository owner/name;
- workspace path;
- expected branch name;
- PR expectations;
- validation expectations;
- GitHub coordination rules;
- reminder that the run is unattended.

Continuation prompts SHOULD be short and include:

- current issue state;
- previous turn outcome;
- reason for continuation;
- remaining turn budget.

## 13. PR Delivery Contract

The first implementation MAY choose one of two strategies:

1. `agent_managed_pr`: prompt Claude to commit, push, and open/update the PR.
2. `symphony_managed_pr`: Symphony inspects workspace changes and creates or
   updates the PR after Claude finishes.

The chosen strategy MUST be documented. The default SHOULD be
`agent_managed_pr` for MVP speed, with tests around prompt expectations and
artifact capture.

PR requirements:

- Branch name follows configured prefix.
- PR title includes issue identifier or `Fixes #<number>` style reference.
- PR body includes summary, validation, artifacts/limitations, and linked issue.
- Existing open PR for the issue/branch is updated rather than duplicated.
- Failed or partial runs MUST NOT silently create misleading PRs.

## 14. Orchestrator Lifecycle

Startup:

1. Load workflow config.
2. Validate GitHub credentials and repository access.
3. Normalize workspace and artifact paths.
4. Recover persisted session/run records.
5. Clean terminal workspaces when configured.
6. Start poll loop.

Poll cycle:

1. Fetch candidate issues.
2. Reconcile active workers.
3. Dispatch eligible issues up to `agent.max_concurrency`.
4. Enqueue due retries.
5. Emit runtime snapshot logs.

Worker attempt:

1. Claim issue.
2. Create or reuse workspace.
3. Prepare git branch.
4. Render prompt.
5. Run `before_run`.
6. Start or restore Claude session.
7. Stream events until terminal turn state.
8. Refresh issue and PR state.
9. Continue on same session while active and under `agent.max_turns`.
10. Ensure PR according to delivery strategy.
11. Run `after_run`.
12. Release claim and update issue/PR comments.

## 15. Reconciliation

The orchestrator SHOULD periodically refresh active issue states.

If issue is closed:

- cancel active worker;
- release claim;
- preserve workspace unless cleanup policy deletes it.

If issue loses required label or gains excluded label:

- cancel active worker;
- release claim;
- do not mark failure unless configured.

If linked PR is merged:

- release claim;
- optionally add done label;
- optionally clean workspace.

If a run is stalled:

- cancel provider;
- write terminal artifact;
- schedule retry if retryable.

## 16. Retry And Recovery

Retryable failures:

- GitHub transport failure;
- claim comment failure after label claim succeeded;
- workspace population failure caused by transient git/network errors;
- provider startup failure;
- provider crash;
- turn timeout;
- stall timeout.

Non-retryable failures:

- invalid workflow config;
- missing credentials;
- permission denied;
- repository not found;
- workspace path escaping root;
- unsupported provider;
- prompt rendering failure.

Restart recovery:

- No live worker process is assumed recoverable after daemon restart.
- Preserved workspaces MUST be reused.
- Session records SHOULD be inspected.
- If an issue still has `claim_label` from a dead run, Symphony SHOULD either
  reclaim it when metadata proves ownership or mark it blocked for operator
  review.
- Retry queue may be reconstructed from artifacts and issue labels/comments.

## 17. Logging And Artifacts

Structured logs SHOULD be JSONL.

Required log context:

- `timestamp`
- `level`
- `event`
- `issue_identifier`
- `session_id`
- `provider`
- `provider_session_id`
- `workspace_path`
- `attempt`

Per-attempt artifact directory:

```text
<claude.artifact_store>/<owner>_<repo>_<issue_number>/<attempt>/
```

Recommended files:

- `request.json`
- `session.json`
- `github.json`
- `events.jsonl`
- `provider-stderr.log`
- `provider-stdout.log`, if separate from events
- `usage.json`
- `terminal.json`

Secrets MUST be redacted.

## 18. Optional Tools

Symphony MAY expose client-side tools to Claude Code.

First standardized optional tool:

- `github_graphql`

The tool executes one GitHub GraphQL operation using Symphony's configured
GitHub credentials.

Requirements:

- exactly one GraphQL operation per call;
- optional JSON object variables;
- structured success/failure output;
- no raw credential exposure;
- unsupported input fails without stalling the session.

## 19. Optional Status Surface

A status surface is optional for the first implementation.

If implemented, useful endpoints are:

- `GET /health`
- `GET /api/state`
- `POST /api/refresh`

The status surface MUST be observational/control-plane only. It MUST NOT become
required for core scheduling.

## 20. Security And Trust

Symphony executes coding agents against repositories and issue content that may
be adversarial. Implementations MUST document their trust posture.

Minimum controls:

- workspace root containment;
- path sanitization;
- secret redaction in logs/artifacts;
- explicit Claude permission policy;
- GitHub token scope documentation;
- no raw token exposure to prompts or tools;
- opt-in live integration tests;
- clear behavior for permission prompts and user-input-required states.

Recommended controls:

- use least-privilege GitHub tokens;
- restrict eligible repositories and labels;
- run in trusted local environments first;
- make destructive cleanup opt-in;
- review PRs before merge.

## 21. Testing Requirements

Required test areas:

- workflow parsing and env var resolution;
- config defaults and validation;
- unknown prompt variables failing closed;
- workspace path sanitization and root containment;
- lifecycle hook success/failure/timeout;
- fake GitHub tracker candidate fetch and reconciliation;
- claim/release conflict behavior;
- fake provider session start/send/restore/cancel/close;
- multi-turn continuation on the same session;
- orchestrator max concurrency;
- retry backoff;
- stall timeout;
- terminal artifact writing and redaction;
- skipped live GitHub tests;
- skipped live Claude tests.

Recommended env gates:

- `SYMPHONY_RUN_GITHUB_INTEGRATION=1`
- `SYMPHONY_RUN_CLAUDE_INTEGRATION=1`
- `GITHUB_TOKEN`

Skipped tests MUST report as skipped.

## 22. Milestones

### M0: Design

- Finalize this spec.
- Document Claude provider architecture.
- Expand GitHub issues into implementation-ready tickets.

### M1: Core Skeleton

- Python project scaffold.
- Workflow/config loader.
- Workspace manager.
- Fake GitHub tracker.
- Fake provider.
- Orchestrator tests.

### M2: GitHub And Claude Integrations

- GitHub tracker adapter.
- Claude Code provider.
- Cancellation, timeout, cleanup.
- Artifact logging.

### M3: Local E2E

- Run one real GitHub issue through Claude Code.
- Produce PR and artifact bundle.
- Update runbook.

### M4: Hardening

- Provider contract fixtures.
- Retry/resume fixtures.
- Optional `github_graphql` tool.
- Optional status API if needed.

