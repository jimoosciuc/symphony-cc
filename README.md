# Symphony

This repository is `symphony-cc`, the Claude Code first and GitHub first
implementation plan for Symphony. The product name, Python package name, and
CLI name remain `symphony`.

The first milestone is design-only:

- adapt the original Symphony service contract to Claude Code;
- define a provider-neutral agent runtime boundary;
- make Claude Code sessions, streaming input, resume, cancellation, GitHub issue
  coordination, pull requests, and logs first-class in the spec;
- create implementation issues before writing runtime code.

`SPEC.md` is the source of truth for the new system. Implementation should not
begin until the design PR is reviewed.

## Intended Shape

```text
GitHub issue
  -> Symphony daemon
  -> deterministic issue workspace
  -> Claude Code session via provider boundary
  -> GitHub pull request
  -> normalized event stream
  -> JSONL logs and run artifacts
```

The first implementation will be Python, with package and CLI name `symphony`.

## Non-Goals For The Design PR

- No runtime implementation.
- No Codex provider.
- No dashboard.
- No database.
- No migration from the existing Elixir codebase.

## Future CLI Target

```bash
symphony run --workflow WORKFLOW.md
```

## Development

Symphony targets Python `>=3.10`. The repo is `symphony-cc`; the package and
CLI are `symphony`.

### Install

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

### Run the CLI

```bash
symphony --help
symphony --version
symphony run --workflow WORKFLOW.example.md --once
```

The `run` subcommand loads the workflow, performs restart recovery, then runs
one poll tick with `--once` or keeps polling without it. Long-running mode
reloads valid `WORKFLOW.md` edits on poll-cycle boundaries; invalid edits keep
the last-known-good config active for current workers and pause new dispatch
until the file is fixed. Reload evidence is written under
`<claude.artifact_store>/_retention_reports/_reload_events.jsonl`.

Runtime status is available as a read-only in-memory Python API:
`orchestrator.status_snapshot()` or `symphony.status.build_status_snapshot(...)`.
It redacts configured secret keys and is intended as the data source for the
future dashboard, not as a write-control surface.

For a human-readable local view, render that snapshot with
`symphony.dashboard.render_dashboard_html(...)` or
`write_dashboard_html(...)`. The renderer produces static HTML only; it does
not start a server or add write controls.

Usage accounting is best-effort and provider-dependent. When normalized usage
events are present, Symphony writes `usage.json`, includes usage in
`terminal.json`, and surfaces totals in the status snapshot/dashboard.

### Tests

```bash
make ci
```

`make ci` is the same validation entry point used by required PR CI. It runs
`ruff check src/ tests/` and the default pytest suite, including fake
provider/tracker integration tests. Live integration tests are skipped unless
their opt-in environment variables are set:

- `SYMPHONY_RUN_GITHUB_INTEGRATION=1`
- `SYMPHONY_RUN_GRAPHQL_TOOL_INTEGRATION=1`
- `SYMPHONY_RUN_CLAUDE_INTEGRATION=1`
- `SYMPHONY_RUN_FULL_E2E=1`
- `SYMPHONY_RUN_REMOTE_CLAUDE_E2E=1`
- `SYMPHONY_RUN_CONCURRENCY_E2E=1`

Live tests can be run locally through `make live-integration` or individually
with `make live-github`, `make live-graphql`, `make live-claude`,
`make live-remote`, `make live-remote-claude`, `make live-e2e`, and
`make live-concurrency-e2e`. They are also available in GitHub Actions through
the manual `live-integration` workflow; they are not required for default PR CI
because they need credentials and real external services.

#### Full E2E Harness

The full end-to-end harness (`make live-e2e`) exercises the complete production
path: GitHub issue discovery → claim → workspace.populate=git → Claude Code
session → branch/commit/PR → evidence detector → terminal artifacts. This test:

- Requires `SYMPHONY_RUN_FULL_E2E=1`, `GITHUB_TOKEN`, authenticated `claude` CLI,
  and `claude-agent-sdk`
- Uses `claude-opus-4-7` by default (override with `SYMPHONY_CLAUDE_TEST_MODEL`)
- Discovers a `symphony-ready` issue or uses `SYMPHONY_E2E_TEST_ISSUE=<number>`
- Records evidence to `evidence/e2e_evidence_issue_<N>.json` including:
  - `task_outcome` (completed_with_pr, completed_no_pr_declared, etc.)
  - `outcome_decided_by` (detector, timeout, error)
  - `permission_denials_count`
  - `branch_name`, `pr_number`, `pr_url`
  - `provider_session_id`, `terminal_json_path`

Run with: `make live-e2e`

This harness is opt-in and skipped by default to keep `make ci` fast and offline.

#### Multi-Issue Concurrency E2E Harness

The concurrency harness (`make live-concurrency-e2e`) runs a real orchestrator
tick with `agent.max_concurrency: 2` against two configured GitHub issues and
Claude Code sessions. It records dispatched/finished issues, status snapshots,
artifact directories, session ids, and task outcomes to local evidence.

Required environment:

- `SYMPHONY_RUN_CONCURRENCY_E2E=1`
- `GITHUB_TOKEN`
- `SYMPHONY_CONCURRENCY_E2E_ISSUES=<issue1>,<issue2>`

Optional environment:

- `SYMPHONY_GITHUB_TEST_OWNER`
- `SYMPHONY_GITHUB_TEST_REPO`
- `SYMPHONY_CLAUDE_TEST_MODEL`
- `SYMPHONY_CONCURRENCY_E2E_PERMISSION_MODE`

The test issues should be isolated, disposable tasks that can safely run in
parallel. Default CI only verifies the harness configuration contract and skip
gate.

#### Remote Claude E2E Harness

The remote Claude harness (`make live-remote-claude`) stages a remote dispatch
snapshot, uploads it over SSH, runs `symphony-worker` without `--fake`, and
records remote worker events plus `terminal.json` evidence.

Required environment:

- `SYMPHONY_RUN_REMOTE_CLAUDE_E2E=1`
- `GITHUB_TOKEN` for read-side PR evidence lookup
- `SYMPHONY_REMOTE_TEST_HOST`
- `SYMPHONY_REMOTE_WORKSPACE_ROOT`
- `SYMPHONY_REMOTE_ARTIFACT_ROOT`
- `SYMPHONY_REMOTE_SESSION_STORE`
- `SYMPHONY_REMOTE_GIT_TOKEN`

The remote host must have `symphony-worker`, `claude`, authenticated Claude
credentials, and writable workspace/artifact/session roots. The harness remains
opt-in; default CI only verifies payload assembly and redaction boundaries.

### Lint and format

```bash
make lint
ruff format .
```
