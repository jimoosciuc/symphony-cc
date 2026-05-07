# Issue Plan

These issues define the first implementation path for `symphony-cc`. Each issue
should be executable by an unattended coding agent after reading `SPEC.md`,
`docs/IMPLEMENTATION_PLAYBOOK.md`, and the issue body.

## Execution Rules

- Work issues in milestone order.
- Do not implement Linear or Codex support.
- Keep package and CLI names as `symphony`.
- Use GitHub issues, labels, comments, and PRs as the primary work surface.
- Treat GitHub Projects as optional metadata, not a required MVP dependency.
- Prefer fakes before live integrations.
- Every implementation issue must include focused tests.
- Real GitHub or Claude tests must skip unless explicitly opted in by env vars.
- Update docs when behavior, config, artifacts, or run commands change.

## Milestone 0: Design

1. [#2] Finalize Claude Code + GitHub first SPEC.md
   - Lock down the full runtime contract.
   - Confirm GitHub is the only required tracker.
   - Confirm Claude Code is the only required provider.
   - Convert every remaining ambiguity into implementation issues.

2. [#3] Document Claude Code provider architecture
   - Map Symphony provider methods to Claude Code SDK behavior.
   - Define session persistence, transcript capture, permissions, cancellation,
     usage, fallback behavior, and known SDK dependencies.

## Milestone 1: Core Skeleton

3. [#4] Scaffold Python package and CLI
   - Add project tooling, package layout, and a minimal CLI.
   - The CLI should be stable enough for later issues to extend.

4. [#5] Implement workflow and config loading
   - Parse `WORKFLOW.md`.
   - Resolve env vars.
   - Render prompt templates.
   - Validate GitHub/Claude/workspace config.

5. [#6] Implement workspace manager
   - Deterministic issue workspace paths.
   - Root containment.
   - Git workspace preparation boundary.
   - Lifecycle hooks.

6. [#7] Implement orchestrator with fake GitHub tracker and fake provider
   - Prove the core daemon flow before real integrations.
   - Include multi-turn fake provider behavior.

## Milestone 2: Integrations

7. [#8] Implement GitHub tracker and PR coordination
   - Candidate issue fetch.
   - Claim/release labels and comments.
   - Linked PR discovery.
   - Branch/PR creation policy boundary.

8. [#9] Implement Claude Code provider
   - Long-lived Claude sessions, streaming input, normalized events, persisted
     session metadata, transcripts, and artifacts.

9. [#10] Implement cancellation, timeout, and cleanup semantics
   - Stall timeout, turn timeout, interrupt/cancel, crash handling, terminal
     artifacts.

## Milestone 3: E2E

10. [#11] Run local GitHub issue + Claude Code E2E
    - Validate one real GitHub issue and produce a PR/evidence bundle.

## Milestone 4: Hardening

11. [#12] Add provider contract tests and fixtures
    - Lock down provider behavior independent of Claude SDK drift.

12. [#13] Add optional GitHub GraphQL tool
    - Add a safe tool bridge for GitHub queries or mutations from Claude.

