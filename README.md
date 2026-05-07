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
