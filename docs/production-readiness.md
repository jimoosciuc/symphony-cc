# Production Readiness

This document defines the validation matrix for a trusted production pilot.
Passing default CI is necessary, but it is not enough to call Symphony
production ready because the most important failure modes involve real GitHub,
Claude Code, SSH, and long-running daemon behavior.

Use `docs/production-operations-runbook.md` for host setup, supervisor
configuration, stop/restart/recovery procedures, and pilot go/no-go operations.

## Readiness Levels

| Level | Meaning | Required evidence |
| --- | --- | --- |
| L0: default CI | Offline unit, contract, fake tracker/provider, and static validation pass. | `make ci` is green locally and in required PR CI. |
| L1: live integrations | Individual live GitHub, GitHub GraphQL, Claude, and remote transport checks pass. | `make live-integration` or the matching individual targets pass with real credentials. |
| L2: full local E2E | A real GitHub issue is claimed, Claude Code performs work, a branch/PR is produced, and evidence gates resolve the terminal outcome. | `make live-e2e` writes E2E evidence with `completed_with_pr` or another explicitly accepted terminal outcome. |
| L3: production topology E2E | Remote Claude and multi-issue concurrency are validated against real test issues and a real remote worker host. | `make live-remote-claude` and `make live-concurrency-e2e` pass and produce evidence artifacts. |
| L4: operated pilot | The daemon runs under the chosen supervisor with dashboard/status visibility, restart recovery, cleanup, and operator procedures exercised. | A dated operator report links CI, live evidence, dashboard/status snapshots, and any follow-up issues. |

Do not claim production readiness before L4. L0 through L3 prove behavior in
bounded runs; L4 proves the system can be operated.

## Commands

Default required validation:

```bash
make ci
make failure-drills
make security-audit
```

Individual live targets:

```bash
make live-github
make live-graphql
make live-claude
make live-remote
make live-e2e
make live-remote-claude
make live-concurrency-e2e
```

Full live validation matrix:

```bash
make live-validation
```

`make live-validation` intentionally runs every live harness. The harnesses
remain opt-in and skip when their required environment is absent. Use skipped
results as a setup signal, not as production evidence.

## Manual GitHub Actions

The `live-integration` workflow can be triggered manually with these targets:

| Target | Runs |
| --- | --- |
| `github` | Live GitHub tracker tests. |
| `graphql` | Live GitHub GraphQL tool tests. |
| `claude` | Live Claude provider smoke tests. |
| `remote` | Live remote transport smoke tests. |
| `full-e2e` | Full GitHub issue to Claude PR E2E. |
| `remote-claude` | Real remote worker plus Claude E2E. |
| `concurrency-e2e` | Multi-issue Claude concurrency E2E. |
| `all` or `validation-matrix` | All of the above, in workflow step order. |

## Required Environment

Common:

- `GITHUB_TOKEN`: GitHub API token for tracker/evidence tests.
- `SYMPHONY_GITHUB_TEST_OWNER`: optional owner override, defaults to `jimoosciuc`.
- `SYMPHONY_GITHUB_TEST_REPO`: optional repo override, defaults to `symphony-cc`.
- `SYMPHONY_CLAUDE_TEST_MODEL`: optional Claude model override, defaults to `claude-opus-4-7`.

Full E2E:

- `SYMPHONY_RUN_FULL_E2E=1`
- authenticated `claude` CLI on `PATH`
- installed `claude-agent-sdk`
- `SYMPHONY_E2E_TEST_ISSUE`: optional issue number; otherwise the harness discovers a `symphony-ready` issue.
- `SYMPHONY_E2E_PERMISSION_MODE=bypassPermissions`: recommended for production-readiness proof on trusted test issues.
- `SYMPHONY_E2E_REQUIRE_PR=1`: fail the run unless GitHub-visible PR evidence is detected.
- `SYMPHONY_E2E_PR_DETECT_ATTEMPTS` and `SYMPHONY_E2E_PR_DETECT_INTERVAL_SECONDS`: optional PR evidence retry tuning for GitHub indexing delay.

Concurrency E2E:

- `SYMPHONY_RUN_CONCURRENCY_E2E=1`
- `SYMPHONY_CONCURRENCY_E2E_ISSUES=<issue1>,<issue2>`
- authenticated `claude` CLI on `PATH`
- installed `claude-agent-sdk`

Remote Claude E2E:

- `SYMPHONY_RUN_REMOTE_CLAUDE_E2E=1`
- `SYMPHONY_REMOTE_TEST_HOST`
- `SYMPHONY_REMOTE_WORKSPACE_ROOT`
- `SYMPHONY_REMOTE_ARTIFACT_ROOT`
- `SYMPHONY_REMOTE_SESSION_STORE`
- `SYMPHONY_REMOTE_GIT_TOKEN`
- local `ssh` and `scp`
- remote `symphony-worker`, `claude`, and authenticated Claude credentials

## Evidence To Collect

For every production-readiness run, record:

- commit SHA and workflow file used;
- command or GitHub Actions run URL;
- test issue numbers;
- PR URLs created by the harness;
- evidence JSON paths;
- `terminal.json` paths;
- dashboard URL or saved `/status.json` snapshot;
- skipped live targets and the missing environment that caused each skip;
- follow-up issues for every failed or inconclusive path.

## Go/No-Go Checklist

Go only when all are true:

- `make ci` is green.
- Required live targets for the intended deployment topology pass.
- Full E2E produces GitHub-visible evidence, preferably `completed_with_pr`.
- Remote E2E is run before using remote workers in production.
- Concurrency E2E is run before setting `agent.max_concurrency > 1`.
- Runtime lanes are exercised before using role-specific implementer/reviewer lanes.
- Dashboard/status snapshots show active, retrying, finished, failed, remote, and lane-tagged work clearly enough for operators.
- Secrets are redacted from logs, dashboard HTML, status JSON, and artifacts.
- Any skipped live target is explicitly accepted as out of scope for the deployment.

No-go when any are true:

- Default CI fails.
- A provider failure is reported as successful completion.
- Evidence detection cannot distinguish `completed_with_pr` from no GitHub-visible work.
- Remote worker output exposes tracker credentials.
- Operators cannot find the terminal outcome or artifact path for a run.
- Live tests are skipped by accident rather than an explicit deployment decision.

## Current Gap Statement

As of this validation matrix, Symphony is suitable for controlled pilots after
the relevant L1 through L4 evidence is collected. It is not automatically
production ready because default CI does not exercise real external services,
real SSH hosts, long-running supervision, or operator recovery procedures.
