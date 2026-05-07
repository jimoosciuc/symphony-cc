# Issue Plan

These issues define the first implementation path for `symphony-cc`.

## Milestone 0: Design

1. Finalize Claude Code first SPEC.md
   - Review provider contract.
   - Confirm one issue-scoped Claude session per active worker.
   - Confirm one-shot CLI usage is fallback only.

2. Document Claude Code provider architecture
   - Map Symphony provider methods to Claude Code SDK concepts.
   - Define session store, transcript store, permission policy, cancellation,
     timeout, and usage behavior.

## Milestone 1: Core Skeleton

3. Scaffold Python package and CLI
   - Package name: `symphony`.
   - CLI command: `symphony`.
   - No provider implementation beyond fakes.

4. Implement workflow and config loading
   - YAML front matter.
   - Prompt template rendering.
   - Env var resolution.
   - Validation errors.

5. Implement workspace manager
   - Deterministic issue workspace.
   - Path sanitization.
   - Root containment.
   - Lifecycle hooks.

6. Implement orchestrator with fake tracker/provider
   - Polling.
   - Dispatch.
   - Max concurrency.
   - Retry backoff.
   - Reconciliation.

## Milestone 2: Integrations

7. Implement Linear tracker adapter
   - Candidate fetch.
   - State refresh.
   - Terminal issue fetch.
   - Normalized issue model.

8. Implement Claude Code provider
   - Long-lived session per issue.
   - Streaming input.
   - Streaming normalized events.
   - Session persistence.
   - Transcript/artifact capture.

9. Implement cancellation, timeout, and cleanup
   - Stall timeout.
   - Turn timeout.
   - Interrupt/cancel.
   - Provider process cleanup.

## Milestone 3: E2E

10. Run local Linear + Claude E2E
    - One real issue.
    - Artifact bundle.
    - Known limitations.
    - README runbook update.

## Milestone 4: Hardening

11. Add provider contract tests
    - Fake provider multi-turn behavior.
    - Retry resume behavior.
    - Crash recovery using persisted session records.

12. Add optional Linear GraphQL tool
    - Safe one-operation GraphQL execution.
    - Structured success/failure output.
    - No credential exposure.

