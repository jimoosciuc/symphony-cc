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

Live tests can be run locally through `make live-integration` or individually
with `make live-github`, `make live-graphql`, and `make live-claude`. They are
also available in GitHub Actions through the manual `live-integration` workflow;
they are not required for default PR CI because they need credentials and real
external services.

### Lint and format

```bash
make lint
ruff format .
```
