# Implementation Playbook

This playbook exists so each GitHub issue can be claimed and completed without
rediscovering the whole architecture.

## Canonical Architecture

```text
symphony run --workflow WORKFLOW.md
  -> WorkflowLoader
  -> Config
  -> Orchestrator
      -> GitHubTracker
      -> WorkspaceManager
      -> AgentProvider
          -> ClaudeCodeProvider
      -> PRCoordinator
  -> JSONL logs and run artifacts
```

The first implementation is Python. The repository is `symphony-cc`, but the
package and CLI are `symphony`.

## Suggested Package Layout

```text
src/symphony/
  __init__.py
  main.py
  cli.py
  config.py
  workflow.py
  models.py
  events.py
  logging.py
  artifacts.py
  workspace.py
  orchestrator.py
  retry.py
  github/
    __init__.py
    tracker.py
    pr.py
    client.py
  provider/
    __init__.py
    base.py
    fake.py
    claude_code.py
  tools/
    __init__.py
    github_graphql.py
tests/
  fixtures/
  test_workflow.py
  test_workspace.py
  test_orchestrator.py
  test_provider_contract.py
  test_github_tracker.py
```

Issues may adjust this layout if implementation pressure reveals a cleaner
shape, but keep module boundaries clear.

## Core Data Models

Use typed models or dataclasses for:

- `Issue`
- `PullRequest`
- `WorkflowConfig`
- `TrackerConfig`
- `GitHubConfig`
- `AgentConfig`
- `WorkspaceConfig`
- `ClaudeConfig`
- `Workspace`
- `SessionRecord`
- `AgentEvent`
- `RunArtifact`
- `RetryState`

Avoid passing unstructured dictionaries across layer boundaries except for
provider-specific raw payloads under explicit `raw` or `payload` fields.

## Provider Boundary

The orchestrator should only know this behavior:

```text
start_session(issue, workspace_path, config) -> SessionRecord
send_input(session, message) -> stream[AgentEvent]
interrupt(session) -> AgentEvent
cancel(session) -> AgentEvent
close(session) -> None
restore(session_record) -> SessionRecord
```

`start_session` and `restore` create/resume the provider session and return
a `SessionRecord` only — they do not take the first prompt and do not stream
events. The orchestrator runs every turn (first and continuation) through
`send_input`. The first `send_input` after `start_session` emits
`session_started`; the first after `restore` emits `session_restored`.

The fake provider must implement the same interface before the Claude provider
exists. Orchestrator tests should use the fake provider by default.

## GitHub Boundary

The orchestrator should only know this tracker behavior:

```text
fetch_candidate_issues(config) -> list[Issue]
fetch_issues_by_numbers(numbers) -> list[Issue]
claim_issue(issue, run_metadata) -> ClaimResult
release_issue(issue, reason) -> ReleaseResult
find_linked_pull_requests(issue) -> list[PullRequest]
create_or_update_progress_comment(issue, body) -> Result
create_or_update_pr_link_comment(issue, pr) -> Result
```

PR behavior may live in `github/pr.py` or be agent-managed through prompt
policy, but the selected strategy must be documented.

## Workflow Reload Boundary

Implement workflow reload as a small boundary around the existing workflow
loader/config layer. The orchestrator should only know this behavior:

```text
load_initial(workflow_path) -> WorkflowSnapshot
maybe_reload(previous_snapshot) -> ReloadResult
```

Recommended model:

- `WorkflowSnapshot`
  - parsed prompt template
  - typed `WorkflowConfig`
  - workflow path
  - file identity metadata: mtime, size, optional inode when available
  - `revision`
  - `loaded_at`
- `ReloadResult`
  - `current_snapshot`
  - `changed`
  - `reloaded`
  - `error`
  - `dispatch_paused`

Design rules for implementation issues:

- The first implementation should poll workflow file metadata at the start of
  each orchestrator poll cycle; do not add a required file watcher.
- Active workers keep the `WorkflowSnapshot` captured at worker-attempt start.
  Do not mutate an active Claude session's prompt/config after reload.
- New dispatches use the latest last-known-good snapshot.
- Retry attempts started after reload use the latest last-known-good snapshot
  while preserving retry state and session metadata.
- If a changed workflow file fails parse/validation, keep the old snapshot for
  active-worker reconciliation but pause new dispatch by default.
- Surface reload success/failure through structured logs and artifacts.
- Keep the status API optional; reload must not depend on dashboard work.

## Security Profiles (M7.1)

Security profiles validate `claude.permission_mode` against operator intent at
config load time. Profiles are NOT host-level sandbox guarantees; they describe
intended trust boundaries and reject obviously unsafe configurations.

Config field: `security.profile` (optional, defaults to `conservative`)

Supported profiles:

- **conservative** (default): Human-safer profile compatible with `acceptEdits`.
  Permission denials remain operator-visible through terminal outcome gates.
  Recommended for most workflows.
- **trusted_unattended**: Intended for trusted repos/issues on trusted hosts.
  Allows unattended work and may use `bypassPermissions` when explicitly
  configured. Emits high-risk warning when combined with `bypassPermissions`.
- **restricted**: Read-only / no privileged tool posture. Rejects
  `bypassPermissions`. Task completion may require handoff or blocked outcomes.
  Use when Claude should not execute privileged operations.

Validation rules:

- `restricted` + `claude.permission_mode: bypassPermissions` is a config error.
- `trusted_unattended` + `bypassPermissions` is allowed but emits a high-risk warning.
- `conservative` + `bypassPermissions` keeps the existing `bypassPermissions` warning.
- Unknown profile names are config errors.

Implementation notes:

- Validation happens in `build_config()` via `_validate_security_profile()`.
- Cross-field validation runs after all section builders complete.
- Profiles do not affect runtime behavior; validation is config-time only.
- The provider does not enforce profiles; it passes `permission_mode` unchanged.

## Remote Worker Execution (M7.4)

Remote execution allows the orchestrator to dispatch work to remote hosts via SSH. The design is in `docs/remote-worker-design.md`.

Module boundaries:

- **Coordinator (Orchestrator)**: Owns tracker interactions, issue claims, remote dispatch, artifact collection, status snapshot. Runs on local host.
- **Remote Worker**: New CLI (`symphony-worker`) that runs on remote hosts. Receives dispatch commands, creates workspaces, runs provider sessions, streams status events, writes artifacts locally.
- **Artifact Collector**: New component on coordinator. Copies artifacts from remote worker via scp/rsync, applies redaction, writes to local artifact store.
- **SSH Client**: Thin wrapper around SSH command execution. Handles connection, command dispatch, stdout streaming.

Key contracts:

- Remote worker streams JSONL status events to stdout: `worker_started`, `workspace_ready`, `session_started`, `heartbeat`, `turn_completed`, `worker_completed`.
- Coordinator reads events line-by-line and updates in-memory worker state.
- Remote worker never calls tracker; coordinator owns all tracker operations.
- Config snapshot transmitted to remote worker includes `tracker.token` (for git auth), `security.profile`, and all workflow config.
- Artifacts collected after worker completion; coordinator applies redaction before local write.

Testing strategy:

- Fake remote worker protocol tests with mocked SSH transport (default CI).
- Opt-in live remote tests require `SYMPHONY_RUN_REMOTE_INTEGRATION=1` and SSH-accessible test host.

Implementation phases:

1. Protocol and fake tests (#109): Add `RemoteConfig`, `symphony-worker` CLI stub, fake remote worker, SSH client wrapper, artifact collector, fake protocol tests.
2. SSH transport and live tests (#110): Implement SSH execution, event streaming, artifact collection, opt-in live tests.
3. Orchestrator integration (#111): Add remote execution decision logic, wire into dispatch flow, update status snapshot and dashboard.

## Event Contract

Every provider event should include:

- `event`
- `timestamp`
- `session_id`
- `provider`
- `provider_session_id`
- `issue_identifier`
- `attempt`
- `payload`

Start with these event names:

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

Do not leak raw Claude SDK event shapes above the provider layer.

## Artifact Contract

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
- `provider-stdout.log`
- `usage.json`
- `terminal.json`

Artifacts must redact secrets. A test should cover obvious token-looking values
before the first real integration milestone.

## Testing Rules

Use focused tests for each issue. Keep live integrations opt-in.

Recommended env gates:

- `SYMPHONY_RUN_GITHUB_INTEGRATION=1`
- `SYMPHONY_RUN_CLAUDE_INTEGRATION=1`
- `GITHUB_TOKEN`

Skipped tests should report as skipped, not silently pass.

## Definition Of Done For Implementation Issues

- Code is implemented in the expected module boundary.
- Focused tests cover success and failure paths.
- Existing tests pass.
- README or docs are updated for new CLI/config/runtime behavior.
- Live tests are skipped unless credentials and opt-in env vars exist.
- No Linear or Codex provider behavior is introduced.
