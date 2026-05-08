# Remote Worker Execution Design (M7.4)

## Status

Design document for #108. Defines the remote worker execution protocol and boundaries before implementation.

## Goals

Enable Symphony to dispatch Claude Code sessions to remote worker hosts while preserving the GitHub-first, Claude Code-first architecture. Remote execution must:

- Support trusted remote hosts (SSH-accessible Linux/macOS machines)
- Preserve existing terminal evidence gates, cleanup/retention, workflow reload, status snapshot, dashboard, usage accounting, and security profiles
- Avoid requiring Kubernetes, databases, Linear, or Codex
- Keep secrets (tracker tokens, Claude auth, SSH keys) out of prompts/logs/artifacts

## Architecture Overview

### Execution Modes

Symphony supports two execution modes:

1. **Local execution** (current): Orchestrator runs provider sessions directly on the same host
2. **Remote execution** (new): Orchestrator dispatches work to remote worker hosts via SSH

The orchestrator decides per-issue which mode to use based on workflow config.

### Component Responsibilities

**Coordinator (Orchestrator)**:
- Polls tracker for candidate issues
- Claims issues and decides local vs remote execution
- For remote: establishes SSH connection, sends dispatch command, monitors status events
- Collects artifacts from remote workers
- Releases issues and updates tracker state
- Maintains status snapshot and retry queue

**Remote Worker**:
- Receives dispatch commands via SSH
- Creates/reuses workspace using WorkspaceManager
- Runs ClaudeCodeProvider session
- Writes artifacts locally
- Streams status events back to coordinator
- Cleans up on completion or cancellation

**Tracker (GitHubTracker)**:
- Unchanged; coordinator continues to own all tracker interactions
- Remote workers never call tracker directly

**WorkspaceManager**:
- Runs on remote worker for remote execution
- Handles git clone/fetch, hooks, cleanup per existing contract
- Remote `workspace.root` is independent of coordinator's local root

**Provider (ClaudeCodeProvider)**:
- Runs on remote worker for remote execution
- Uses remote worker's Claude CLI auth
- Writes events.jsonl, terminal.json, etc. to remote artifact directory

**Artifact Collector**:
- New component on coordinator
- Copies artifacts from remote worker to coordinator's `claude.artifact_store`
- Applies redaction before writing locally

### Transport Protocol

Remote execution uses SSH as the transport. The coordinator runs:

```bash
ssh user@remote-host symphony-worker dispatch \
  --issue-identifier OWNER/REPO#N \
  --attempt N \
  --workspace-root /remote/workspace/root \
  --artifact-root /remote/artifact/root \
  --session-store /remote/session/store \
  --config-snapshot /tmp/snapshot.json
```

The `symphony-worker` CLI (new) runs on the remote host and:
1. Loads the config snapshot
2. Creates/reuses workspace
3. Starts provider session
4. Streams status events to stdout (JSONL)
5. Writes artifacts locally
6. Exits with code 0 (success) or non-zero (failure)

### Status Events

Remote worker streams JSONL events to stdout:

```json
{"event": "worker_started", "timestamp": "...", "worker_id": "...", "issue_identifier": "..."}
{"event": "workspace_ready", "timestamp": "...", "workspace_path": "..."}
{"event": "session_started", "timestamp": "...", "session_id": "...", "provider_session_id": "..."}
{"event": "heartbeat", "timestamp": "...", "session_id": "..."}
{"event": "turn_completed", "timestamp": "...", "session_id": "...", "payload": {...}}
{"event": "worker_completed", "timestamp": "...", "exit_code": 0, "artifacts_ready": true}
```

Coordinator reads stdout line-by-line and:
- Updates in-memory worker state
- Forwards provider events to status snapshot
- Detects stalls (no heartbeat within `stall_timeout_ms`)
- Handles worker failures (non-zero exit, SSH disconnect)

### Auth and Secret Boundaries

**Tracker token**:
- Coordinator holds `tracker.token`
- Remote worker never sees tracker token
- Coordinator makes all tracker API calls

**Claude CLI auth**:
- Remote worker uses its own Claude CLI login (`claude login` run on remote host)
- Coordinator never sends Claude credentials over SSH
- Each remote host must have Claude CLI installed and authenticated

**SSH credentials**:
- Coordinator uses SSH key or password to connect to remote hosts
- SSH config managed outside Symphony (e.g., `~/.ssh/config`)
- Workflow config specifies remote host via `remote.host` field (new)

**Git credentials**:
- Remote worker uses `tracker.token` for git clone/fetch auth
- Coordinator sends token via config snapshot (encrypted or via secure channel)
- Token never persisted in remote workspace `.git/config`

### Workspace Semantics

**Remote `workspace.root`**:
- Specified in workflow config: `remote.workspace_root`
- Independent of coordinator's local `workspace.root`
- Remote worker creates per-issue directories: `<remote.workspace_root>/<owner>_<repo>_<n>/`

**Populate behavior**:
- Remote worker runs `workspace.populate: git` using same logic as local execution
- Git clone/fetch uses `tracker.token` for auth
- Hooks (`after_create`, `before_run`, etc.) run on remote worker

**Cleanup interaction**:
- Remote worker respects `workspace.cleanup` config from snapshot
- Cleanup executor runs on remote worker after session completes
- Coordinator does not clean up remote workspaces directly

**Path safety**:
- Remote worker validates workspace paths stay inside `remote.workspace_root`
- Sanitization rules match local execution (alphanumeric + `._-`)

**Deterministic reuse**:
- Remote worker reuses existing workspace if present
- Destructive refresh (`git reset --hard`, `git clean -fdx`) before each dispatch

### Artifact Collection

**Artifacts produced remotely**:
- `events.jsonl`
- `request.json`
- `session.json`
- `terminal.json`
- `usage.json`
- `provider-stderr.log`
- `provider-stdout.log`

**Collection process**:
1. Remote worker writes artifacts to `<remote.artifact_root>/<owner>_<repo>_<n>/<attempt>/`
2. On worker completion, coordinator runs `scp` or `rsync` to copy artifacts
3. Coordinator applies redaction before writing to local `claude.artifact_store`
4. Remote artifacts remain on remote host (cleanup per remote retention policy)

**Partial artifacts on failure**:
- If worker fails mid-session, coordinator attempts to collect partial artifacts
- Missing artifacts are logged but do not block issue release
- Terminal outcome detector works with available artifacts

**Redaction**:
- Coordinator redacts secrets from collected artifacts before local write
- Redaction keys from `logging.redact_keys` plus tracker token, SSH keys
- Remote worker does not redact (coordinator is single redaction point)

### Failure Taxonomy

**Infrastructure failures** (coordinator responsibility):
- SSH connection failure: retryable, does not mark issue blocked
- SSH authentication failure: retryable with backoff
- Remote host unreachable: retryable
- `symphony-worker` CLI not found: operator error, mark issue blocked

**Workspace failures** (remote worker responsibility):
- Git clone/fetch failure: retryable
- Hook timeout: retryable or terminal per hook config
- Workspace path validation failure: terminal, mark issue blocked

**Provider failures** (remote worker responsibility):
- Claude CLI not authenticated: terminal, mark issue blocked
- Session start failure: retryable per retry policy
- Turn timeout: terminal per existing timeout logic
- Permission denied: terminal per existing permission logic

**Task failures** (provider responsibility):
- Test failures, build errors, etc.: terminal per existing evidence gates
- Incomplete work: terminal per existing outcome detector

**Retryability**:
- Infrastructure failures: retry with exponential backoff, do not count against `retry.max_attempts`
- Workspace/provider failures: count against `retry.max_attempts`
- Task failures: terminal, no retry

### Compatibility with Existing Surfaces

**Terminal evidence gates** (SPEC §14):
- Unchanged; coordinator runs outcome detector on collected artifacts
- Remote execution does not bypass evidence gates

**Cleanup/retention** (M5.6, M5.7):
- Remote worker runs cleanup executor per config snapshot
- Coordinator does not manage remote workspace cleanup
- Artifact retention applies to coordinator's local artifact store

**Workflow reload** (M5.10):
- Coordinator reloads workflow on poll-cycle boundaries
- Active remote workers keep captured config snapshot
- New dispatches use latest snapshot

**Status snapshot** (M5.11):
- Coordinator includes remote workers in status snapshot
- Worker state: `remote`, `host`, `workspace_path`, `artifact_path`, `last_heartbeat`
- Remote workers appear in dashboard with remote host indicator

**Dashboard** (M5.12):
- Dashboard shows remote workers with host label
- Artifact links point to coordinator's local artifact store (collected artifacts)

**Usage accounting** (M6):
- Remote worker streams usage events to coordinator
- Coordinator aggregates usage across local and remote workers

**Security profiles** (M7.1, M7.2, M7.3):
- Config snapshot includes `security.profile`
- Remote worker enforces profile per existing validation
- `restricted` profile rejects `bypassPermissions` on remote worker

### Configuration Schema

New workflow config fields:

```yaml
remote:
  enabled: false
  host: user@remote-host
  workspace_root: /remote/symphony/workspaces
  artifact_root: /remote/symphony/artifacts
  session_store: /remote/symphony/sessions
  worker_timeout_ms: 7200000  # 2 hours
  heartbeat_interval_ms: 30000  # 30 seconds
```

**Defaults**:
- `enabled: false` (local execution only)
- `worker_timeout_ms: 7200000` (2 hours)
- `heartbeat_interval_ms: 30000` (30 seconds)

**Validation**:
- `remote.host` required when `remote.enabled: true`
- `remote.workspace_root`, `remote.artifact_root`, `remote.session_store` required when enabled
- SSH connectivity test on workflow load (optional, warn if unreachable)

### Testing Strategy

**Fake remote worker protocol tests**:
- Mock SSH transport
- Fake remote worker that streams synthetic events
- Test coordinator's event handling, artifact collection, failure detection
- Test retry logic for infrastructure vs workspace vs provider failures
- Test status snapshot includes remote workers

**Opt-in live remote tests**:
- Require `SYMPHONY_RUN_REMOTE_INTEGRATION=1`
- Require SSH-accessible test host (e.g., localhost, Docker container)
- Test end-to-end dispatch, artifact collection, cleanup
- Test SSH connection failures, worker crashes, timeout handling

**No live tests in default CI**:
- Remote execution tests are opt-in only
- Default `make ci` runs fake protocol tests only

## MVP Scope

**In scope**:
- SSH transport to single remote host
- Config snapshot transmission
- Status event streaming
- Artifact collection via scp/rsync
- Infrastructure failure retry
- Compatibility with existing surfaces

**Out of scope** (defer to post-MVP):
- Multiple remote hosts / load balancing
- Remote host health checks / auto-failover
- Remote workspace pre-warming
- Streaming artifact collection (wait for worker completion)
- Remote worker auto-scaling
- Container-based remote workers
- Remote worker observability dashboard

## Implementation Phases

**Phase 1: Protocol and fake tests** (#109, depends on #108):
- Add `RemoteConfig` dataclass
- Add `symphony-worker` CLI stub
- Add fake remote worker for tests
- Add coordinator SSH client wrapper
- Add artifact collector
- Add fake protocol tests

**Phase 2: SSH transport and live tests** (#110, depends on #109):
- Implement SSH command execution
- Implement status event streaming
- Implement artifact collection via scp
- Add opt-in live remote tests

**Phase 3: Integration with orchestrator** (#111, depends on #110):
- Add remote execution decision logic to orchestrator
- Wire remote worker into dispatch flow
- Update status snapshot to include remote workers
- Update dashboard to show remote workers

## Security Considerations

**Secrets in transit**:
- Config snapshot may contain `tracker.token`
- Use SSH encryption for config transmission
- Consider encrypting config snapshot payload

**Secrets at rest**:
- Remote worker writes config snapshot to `/tmp/snapshot-<uuid>.json`
- Delete snapshot after worker completes
- Ensure snapshot has restrictive permissions (0600)

**Artifact redaction**:
- Coordinator is single redaction point
- Remote artifacts may contain secrets until collected
- Remote host must be trusted (same trust level as local execution)

**SSH key management**:
- Coordinator uses SSH keys from `~/.ssh/` or SSH agent
- SSH keys never sent to remote worker
- Remote host must be in coordinator's `known_hosts`

## Open Questions

1. **Config snapshot encryption**: Should we encrypt the config snapshot payload before sending over SSH, or rely on SSH encryption?
   - Recommendation: Rely on SSH encryption for MVP; add payload encryption post-MVP if needed.

2. **Artifact streaming**: Should we stream artifacts during execution or collect after completion?
   - Recommendation: Collect after completion for MVP; streaming adds complexity.

3. **Remote worker installation**: How do operators install `symphony-worker` on remote hosts?
   - Recommendation: Document manual installation for MVP; add auto-install post-MVP.

4. **Multiple remote hosts**: How do we decide which host to use for a given issue?
   - Recommendation: Single host for MVP; add load balancing post-MVP.

## References

- Parent umbrella: #57
- SPEC.md §8 (Workspace Contract)
- SPEC.md §14 (Terminal Evidence Gates)
- docs/IMPLEMENTATION_PLAYBOOK.md (Provider Boundary, Workspace Boundary)
- M5.6 #65 (Workspace Cleanup)
- M5.10 #70 (Workflow Reload)
- M5.11 #55 (Status Snapshot)
- M7.1 #100 (Security Profiles)
