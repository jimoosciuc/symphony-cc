# Failure Injection Drills

This document maps the production failure modes that must be rehearsed before
calling a Symphony deployment production ready. The default drill target is
offline and deterministic:

```bash
make failure-drills
```

`make failure-drills` is intentionally separate from `make ci`. CI already runs
these tests, but the drill target gives operators a focused command when they
are validating a production change or investigating a reliability regression.

## Offline Drill Matrix

| Scenario | Automated coverage | Expected operator-visible behavior |
| --- | --- | --- |
| Daemon restart with stale or active claims | `tests/test_recovery.py` | Recovery decisions are recorded; stale claims are reconciled without losing artifacts. |
| GitHub 429, 5xx, transport, auth, and malformed response failures | `tests/test_github_tracker.py`, `tests/test_evidence.py` | Rate limits and transient lookup failures do not become misleading completed work. |
| Claude 503 or retryable provider failure | `tests/test_timeouts.py`, `tests/test_routing.py`, `tests/test_evidence.py` | Provider failures and incomplete evidence block or retry; they do not count as completed PR work. |
| Remote SSH failure, timeout, malformed output, and stalled worker signals | `tests/test_remote_ssh.py`, `tests/test_remote_runner.py`, `tests/test_orchestrator_remote.py` | Remote failures release or retry with redacted errors and terminal artifacts. |
| Workspace cleanup after terminal outcomes and cleanup failure handling | `tests/test_cleanup_executor.py` | Cleanup only runs for eligible outcomes and does not mask the task result. |
| Invalid workflow reload while workers are active | `tests/test_workflow_reload.py` | Last-known-good config remains active and new dispatch pauses until the workflow is fixed. |

## Live Drills

Offline drills prove the control flow. Live drills prove the deployment
environment.

Run these only against disposable issues and trusted test repositories:

```bash
make live-e2e
make live-remote-claude
make live-concurrency-e2e
```

Before enabling a production topology:

- run `make live-e2e` before any unattended pilot;
- run `make live-remote-claude` before `remote.enabled: true`;
- run `make live-concurrency-e2e` before `agent.max_concurrency > 1`;
- save `/status.json`, `terminal.json`, evidence JSON, PR URLs, and issue URLs;
- file follow-up issues for every skipped, failed, or inconclusive live path.

## Manual Failure Drills

These drills need real infrastructure and should be run during an operated
pilot window.

### Daemon Restart During Work

1. Start Symphony under the production supervisor.
2. Claim a disposable issue and wait until it appears under `/status.json`.
3. Restart the service.
4. Confirm the issue is represented by an active worker, retry entry, recovery
   decision, linked PR, or terminal artifact.
5. Confirm no issue is left indefinitely with only `symphony-running`.

### GitHub Rate Limit Or 5xx

1. Use a test token or mocked proxy that can force 429/5xx responses.
2. Confirm logs and `/status.json` show retry or blocked state.
3. Confirm PR evidence lookup failures produce `unknown` or retry behavior, not
   `completed_with_pr`.
4. Restore normal GitHub access and confirm the next tick recovers.

### Claude 503 Or Provider Failure

1. Run against a disposable issue with provider failure injection, proxy, or a
   known failing Claude configuration.
2. Confirm `terminal.json` records failure or cancellation.
3. Confirm the issue is retried or blocked with artifact evidence.
4. Confirm a successful-looking Claude message without GitHub evidence does not
   mark the issue done.

### Remote SSH Disconnect

1. Start a remote worker live E2E against a disposable issue.
2. Interrupt SSH or stop the remote worker process.
3. Confirm the coordinator records a remote failure, redacts stderr, and keeps
   local artifacts.
4. Re-run `make live-remote-claude` before returning remote execution to
   production.

### Disk Pressure Or Cleanup Failure

1. Use a test host or mount with constrained space.
2. Run a disposable issue until artifacts are written.
3. Trigger cleanup or artifact retention in dry-run mode first.
4. Confirm cleanup errors are visible and do not overwrite terminal outcomes.

## Evidence To Attach

For each drill, attach:

- commit SHA;
- command or GitHub Actions run URL;
- issue and PR URLs;
- `/status.json` snapshot;
- relevant `terminal.json`;
- artifact directory path;
- skipped live targets and why they were accepted;
- follow-up issues for unresolved gaps.

## Pass Criteria

A drill passes only when:

- no provider or infrastructure error is reported as completed work;
- every failed path has a retry, block, release, recovery, or terminal artifact;
- secrets are redacted in logs, dashboard, JSON, and artifacts;
- operators can identify the affected issue, artifact path, and next action.
