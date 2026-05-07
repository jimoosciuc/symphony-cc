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
symphony run --workflow WORKFLOW.example.md
```

The `run` subcommand parses arguments today and exits with a clear
`not yet implemented` message. Behavior will be wired up by the M1 issues
(workflow loader, workspace manager, orchestrator).

### Tests

```bash
pytest
```

Tests live under `tests/` and are pure-unit until later issues add fakes for
GitHub and Claude Code. Live integration tests (when added) will be skipped
unless their opt-in environment variables are set:

- `SYMPHONY_RUN_GITHUB_INTEGRATION=1`
- `SYMPHONY_RUN_CLAUDE_INTEGRATION=1`

### Lint and format

```bash
ruff check .
ruff format .
```
