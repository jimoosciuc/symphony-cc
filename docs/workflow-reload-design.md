# Workflow Reload Design and Lifecycle Boundaries (M5.9)

Issue: #69 (parent: #53). Implementation lands separately under #70 (last-known-good
loader) and #68 (operator evidence + docs).

## Purpose

Symphony reads its `WorkflowConfig` once from `WORKFLOW.md` at process start. Long-lived
operators want to edit the workflow file (toggle a label, raise concurrency, swap a
prompt) WITHOUT restarting the daemon, but reloads must not corrupt active worker state
or silently lose claims. This document defines the semantics, contract, and error
surfaces of a workflow reload BEFORE the poll-cycle reload implementation lands.

## Scope

In scope:
- Which fields are eligible for live reload, and which require restart.
- The trigger model (polling / file watch / explicit signal).
- Last-known-good fallback behavior on a malformed reload.
- Interactions with the retry queue (`Orchestrator.retry_states`) and restart recovery
  (`Orchestrator.recover()` / `claude.retry_resume_policy`).

Out of scope (deferred to other tickets):
- Implementation of the reload runtime (#70).
- Operator dashboard surface (#56).
- Hot-swap of the workspace root, session store, or artifact store (these are too
  invasive for hot reload — see "Restart-required fields" below).

## Field eligibility classes

The reload behavior depends on whether a field affects (a) currently-active workers,
(b) future workers only, or (c) global infrastructure that cannot be safely swapped at
runtime. Every field in `WorkflowConfig` falls into exactly one of these classes.

### Class A — affects active workers (hot-reload allowed, takes effect on next tick)

These changes apply to the currently-running orchestrator loop without disturbing any
in-flight `WorkerState`. Workers that are mid-turn finish their turn under the OLD
config; the next `run_once` tick observes the NEW config.

| Field | Effect |
|---|---|
| `polling.interval_ms` | Next sleep in `run_forever` uses the new interval. |
| `tracker.include_labels` / `exclude_labels` | Next `fetch_candidate_issues` filters on the new label set. Issues already claimed are unaffected even if their labels would now exclude them — the active worker keeps running. |
| `tracker.priority_label` (if defined) | Affects ordering in next dispatch only. |
| `agent.max_concurrency` | If the new value is higher, the next `_dispatch` may claim more issues. If lower, no active worker is killed; the orchestrator simply stops dispatching new work until concurrency falls within the new bound. |
| `retry.*` (backoff, max attempts, jitter) | Applies on the next retry decision; existing `RetryState` carries forward unchanged. New attempts pick up the new backoff. |
| `logging.level` | Applied lazily by the next `_LOG.*` call (Python `logging` is global). |
| `github.blocked_label` / `progress_comment_*` | Applied on next reconcile / progress-comment write. |

### Class B — affects future workers only (hot-reload allowed, deferred to next claim)

These changes affect how a NEW worker is dispatched but cannot retroactively alter an
already-spawned `WorkerState`. The orchestrator uses the NEW value when constructing
the next worker; existing workers keep the OLD value snapshotted on themselves.

| Field | Effect |
|---|---|
| `agent.max_turns` | Per-worker turn budget. New workers get the new budget; existing workers continue with the old. |
| `claude.model` / `permission_mode` / `system_prompt` | Live workers stay bound to the model and prompt they were started with (changing mid-session would corrupt the provider session and Claude's context). New claims pick up the new model. |
| `claude.allowed_tools` / `disallowed_tools` | Same reasoning as model — applied per-session at start. |
| `workflow_path` itself | Reload reads the same file; cannot be re-pointed at runtime (would change the meaning of "this workflow"). |

### Class C — restart-required (reload is a no-op for these fields)

These fields are baked into long-lived process state that cannot be swapped without
risk of data loss or undefined behavior. A reload that changes any of them is REJECTED
(see "Reload trigger and validation" below).

| Field | Why restart |
|---|---|
| `tracker.kind` | Switching tracker implementations (e.g. github → fake) mid-run would orphan all active claims. |
| `tracker.owner` / `tracker.repo` | Workspace dir naming (`{owner}_{repo}_{N}`) is baked into on-disk state and into the closed-PR sweep's prefix anchor (#82). Swapping at runtime would orphan workspaces. |
| `tracker.token` | Reload allowed if the env var resolves to the same secret. A literal change is treated as restart-required to avoid silent re-auth surprises mid-tick. |
| `workspace.root` | Active workers hold workspace paths; swapping would invalidate them. |
| `claude.session_store` / `transcript_store` / `artifact_store` | Restart recovery (`Orchestrator.recover()`) walks these paths at startup; rebinding would split persisted records across two roots and break the recovery contract. |
| `workspace.cleanup.*` | Considered hot-reloadable BY DEFAULT (it's just a per-tick gate read by `Orchestrator._sweep_for_age` and `_sweep_for_closed_prs`). Documented separately in case implementation discovers a gotcha. |
| `claude.retry_resume_policy` | Read at startup by `recover()`. Changing it later is meaningless until the next process start. |

## Reload trigger strategy

Three viable triggers were considered. The chosen approach for #70 is
**poll workflow metadata at poll-cycle start**. Explicit commands and file-watch
libraries are optional follow-ups.

### Chosen: poll workflow metadata at poll-cycle start

At the beginning of each orchestrator poll cycle, before fetching candidate issues,
the orchestrator checks the workflow file metadata observed by the current
last-known-good snapshot. A reload is attempted when the file mtime or size differs
from the last successfully observed value.

On change:

1. Re-parse `workflow_path` using the existing workflow loader path.
2. Resolve environment variables and normalize paths using the same rules as startup.
3. Validate against the eligibility table above. If any Class C field changed,
   reject the reload and emit a WARNING log.
4. If valid, atomically publish a new last-known-good `WorkflowSnapshot` with an
   incremented revision.
5. Append a reload event to `_retention_reports/_reload_events.jsonl`
   (mirroring the artifact-retention reporting pattern from #67) so operators
   can audit which reload changed what.

Rationale:

- It fits the existing daemon cadence; no extra watcher thread, process signal, or
  control-plane API is required.
- It is deterministic in tests because a fake clock and file metadata can drive the
  same path as production.
- It avoids making the optional status/dashboard API part of the scheduling contract.
- Failure mode is obvious: if the changed file is invalid, the daemon keeps running
  active workers on their existing snapshots and pauses new dispatch by default.

### Deferred: explicit command or status API

An explicit reload command or status API endpoint may be added later. It must call
the same validation + publish path as metadata polling and must not introduce a
second reload code path.

### Deferred: file watch

A `watchdog`-style file-watch trigger may be added later as an opt-in setting.
Deferred because:

- Editor save patterns vary (vim writes via swap-and-rename; some editors
  partial-write). Naive watching produces spurious mid-edit reloads.
- Adds a dependency to the core runtime that is not needed for the #70 MVP.
- Polling already reuses the existing `polling.interval_ms` cadence.

If implemented, file watch must use the SAME validation + publish path as metadata
polling — there is exactly one reload code path.

## Last-known-good behavior

A reload attempt has three outcomes. In two of them, the daemon keeps running on its
existing config (the "last-known-good"). The implementer (#70) MUST make this
property load-bearing — never partially apply a reload.

| Outcome | Trigger | Operator-visible surface |
|---|---|---|
| **Accepted** | Reload parsed cleanly AND only Class A/B fields changed. | INFO log line (`workflow reload accepted: changed=[...]`). Reload event appended to `_reload_events.jsonl`. Current last-known-good snapshot published atomically. |
| **Rejected (validation)** | Reload parsed cleanly BUT touched a Class C field, OR failed schema validation. | WARNING log line listing rejected fields. Reload event appended with `outcome: "rejected_validation"` and the field list. Daemon keeps the old config. |
| **Rejected (parse)** | Reload could not parse (missing keys, bad YAML, env var not resolvable). | WARNING log line with the parse exception. Reload event appended with `outcome: "rejected_parse"` and the exception summary. Daemon keeps the old config. |

The reload flow MUST NOT publish a new current snapshot until validation passes. Use
a local `WorkflowSnapshot` candidate; publish by reference assignment after all
validation succeeds.

### Atomicity guarantees

- The reload publish step is a single reference assignment, ordered after a
  successful parse + validation. A reader can observe either the OLD or the NEW
  snapshot but never a half-applied state.
- Active `WorkerState` snapshots no `WorkflowConfig` reference at construction; they
  capture only the specific fields they need (model name, prompt, etc.) at the moment
  of dispatch. This is how Class B fields stay isolated from existing workers.
- The metadata check runs at the start of `run_once`, before fetch/dispatch. A tick
  either uses the previously current snapshot or the newly accepted snapshot; reload
  does not interleave with worker dispatch mid-tick.

## Interactions with the retry queue

`Orchestrator.retry_states: dict[str, RetryState]` carries per-issue backoff history.
A reload that changes `retry.*` fields:

- Does NOT mutate existing `RetryState` entries. Their `attempt_count` /
  `next_attempt_at` are preserved verbatim.
- Affects only the NEXT call to `next_backoff_ms` (which reads from
  `Orchestrator.config.retry`). So a reload that, say, increases `retry.max_attempts`
  immediately allows already-failed issues to retry beyond their old cap; a reload
  that decreases it does NOT retroactively block an issue that's already past the
  new cap (the orchestrator only checks the cap at retry decision time).
- Does NOT touch `Orchestrator.recovery_decisions` (those are restart-recovery
  artifacts, not retry queue state).

## Interactions with restart recovery

`Orchestrator.recover()` runs ONCE at process start, reads
`claude.retry_resume_policy`, and decides what to do with each persisted
`SessionRecord`. A reload that changes the policy AFTER startup is therefore
ineffective until the next restart — this is by design. Recovery is a
process-lifecycle event, not a per-tick decision.

This is also why `claude.session_store` is restart-required (Class C): if it
moved mid-run, the persisted records would split across two directories and the
NEXT `recover()` would only see one half.

## Error surfaces (operator-visible)

The implementer (#68 evidence + docs) MUST surface reload outcomes via at least:

1. **Log lines.** `WARNING` for any rejection; `INFO` for accepted reloads with the
   list of changed fields.
2. **`_reload_events.jsonl`** under `claude.artifact_store/_retention_reports/` (or
   a sibling subdir if cleaner). One JSON line per reload attempt with
   `{timestamp, outcome, changed_fields, errors}`. Mirrors the
   `_retention_reports/` pattern from #67/#89/#90 so operators can grep both with
   the same tooling.
3. **Process exit code unaffected.** A reload rejection does NOT exit. The daemon
   keeps running on the old config. Operators discover problems via logs or the
   reload event file, not via daemon restarts.

There is intentionally NO operator dashboard or status API surface in this design.
Per the issue: "Design does not require dashboard/status API." If a dashboard ships
later (#56), it can read the same `_reload_events.jsonl` source of truth.

## Acceptance-criteria mapping

| Criterion (from #69) | Section |
|---|---|
| Document which workflow changes affect active vs future workers only. | "Field eligibility classes" (A/B/C tables). |
| Document reload trigger strategy: file watch vs polling vs explicit signal/command. | "Reload trigger strategy". |
| Define last-known-good behavior and error surfaces. | "Last-known-good behavior" + "Error surfaces". |
| Define interactions with retry queue and recovery. | "Interactions with the retry queue" + "Interactions with restart recovery". |
| Design does not require dashboard/status API. | Confirmed in "Error surfaces" — log + JSONL only. |

## Non-goals (per #69)

- This document does NOT implement a watcher (deferred to #70).
- This document does NOT specify the exact JSONL schema for `_reload_events.jsonl` —
  the implementer of #68 picks the field names; this design only requires the
  `outcome` and `changed_fields` semantics above.
- Hot-reload of `claude.session_store` and other Class C fields is OUT of scope —
  documented as restart-required.
