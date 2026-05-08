# Terminal outcomes

**Audience:** operators reading `terminal.json` to triage runs;
contributors implementing the M5.2 evidence detector (#60) and the
M5.3 classification (#62); reviewers auditing whether a worker really
completed its task.

**Source of truth:** `SPEC.md` §17 (the artifact schema this document
describes operationally).

**Status:** Design lands here in M5.1 (#61). The evidence-detector
implementation lands in #60. Until then, `task_outcome` is computed
by the derivation fallback in §17.4 and `outcome_decided_by` is
`"derivation"` (or `"unknown"`).

## Two-axis outcome model

A run has two outcomes that can disagree:

| axis | source | example of disagreement |
|---|---|---|
| **Provider outcome** | What the agent SDK returned | Claude's `ResultMessage(is_error=false)` → `terminal_state: completed`. |
| **Task outcome** | What Symphony observed in GitHub-land | No PR exists, no branch was pushed → `task_outcome: incomplete_no_evidence` despite the clean provider state. |

The misleading-success cases that motivate this schema are exactly the
runs where these two axes disagree:

- Claude answered the operator with a clarification message but
  didn't change any files (provider says `completed`, task says
  `incomplete_no_evidence`).
- Claude tried to use Bash, was denied by `permission_mode:
  acceptEdits`, fell back to a "I would do X" message (provider says
  `completed`, task says `incomplete_permission_denied`).
- Claude proposed a fix in conversation but never ran `git push`
  (provider says `completed`, task says `incomplete_no_evidence`).

## How to read `terminal.json`

```jsonc
{
  // -- Provider outcome (existing fields, unchanged) -------------------
  "terminal_state": "completed",
  "reason": "completed",
  "retryable": false,
  "subtype": null,
  "blocked": false,
  "last_event_at": "2026-05-08T17:00:00Z",
  "provider_session_id": "claude-...",
  "error": null,
  "turn_count": 1,
  "permission_denials_count": 0,

  // -- Task outcome (new fields, M5.1) ---------------------------------
  "task_outcome": "completed_with_pr",
  "task_evidence": [
    {
      "type": "branch_pushed",
      "name": "symphony/acme-proj-42",
      "head_sha": "abc123..."
    },
    {
      "type": "pr_linked",
      "url": "https://github.com/acme/proj/pull/99",
      "number": 99,
      "state": "open",
      "created": true
    }
  ],
  "outcome_decided_by": "detector",
  "no_pr_reason": null,
  "task_outcome_recorded_at": "2026-05-08T17:00:01Z"
}
```

## Quick triage

Operators auditing many runs at once can grep by `task_outcome`
without parsing the full artifact:

```bash
# All runs that look successful in provider but produced no PR.
jq 'select(.task_outcome == "incomplete_no_evidence")' \
  .symphony/runs/*/*/terminal.json

# All misleading-success cases combined.
jq 'select(.task_outcome | startswith("incomplete_"))' \
  .symphony/runs/*/*/terminal.json

# Only runs that actually shipped a PR.
jq 'select(.task_outcome == "completed_with_pr")
   | .task_evidence[] | select(.type == "pr_linked").url' \
  .symphony/runs/*/*/terminal.json
```

## `task_outcome` decision flow (M5.2 detector)

```
                                       provider stream done
                                                │
                                                ▼
                              ┌─────────────────────────────────┐
                              │  terminal_state == "completed"? │
                              └────────────┬────────────────────┘
                                  yes      │      no
                              ┌────────────┘      └────────────┐
                              ▼                                ▼
              ┌──────────────────────────────┐     ┌──────────────────────┐
              │ permission_denials_count > 0 │     │ blocked == true?     │
              │  AND no other evidence?      │     └────────┬─────────────┘
              └─────┬────────────────────────┘     yes      │      no
                  yes  │     no                ┌────────────┘      └────────────┐
                       │                       ▼                                ▼
                       │              blocked_operator_required          retryable_failure
                       ▼
        incomplete_permission_denied
                       │
                  (else branch)
                       │
                       ▼
       ┌────────────────────────────────────────────────┐
       │ Run M5.2 evidence detector (#60):              │
       │  1. Look for linked PR (via tracker API)       │
       │  2. Look for pushed branch matching prefix     │
       │  3. Look for `no_pr_declared` sentinel         │
       └─────────────────────┬──────────────────────────┘
                             │
       ┌────────────┬────────┴────────┬───────────────┐
       ▼            ▼                 ▼               ▼
   pr_linked    no_pr_declared   issue_handoff    nothing found
       │            │                 │               │
       ▼            ▼                 ▼               ▼
   completed_   completed_       blocked_         incomplete_
   with_pr      no_pr_declared   operator_        no_evidence
                                 required
```

## Sufficient evidence: what counts

Per SPEC §17.5, the GitHub-issue case is the load-bearing one for the
MVP. The detector's job in #60 is to find one of these per run:

| target outcome | required evidence |
|---|---|
| `completed_with_pr` | One `pr_linked` entry whose `number` matches a PR linked back to the issue (PR body contains `Closes #N`, or the orchestrator's claim/PR-link comment was posted, or the PR was opened against the issue's `expected_branch_name`). |
| `completed_no_pr_declared` | A `no_pr_declared` entry — sourced from a sentinel marker on the issue (e.g. `Symphony-No-PR: typo already fixed in #41`), Claude's final assistant message containing a documented marker, or a workflow-specific terminal marker. `no_pr_reason` MUST be populated. |
| `blocked_operator_required` | An `issue_handoff` entry whose `label_added` matches the configured `blocked_label`. |

Evidence that is **necessary but NOT sufficient by itself**:

- `branch_pushed` without `pr_linked` does not promote a run to
  `completed_with_pr`. A pushed branch with no PR usually means
  Claude got partway and stopped.
- `diff_in_workspace` is purely informational. Local edits never
  reach GitHub; the detector records them so operators can see
  "Claude did edit files locally" when triaging an
  `incomplete_no_evidence` run, but they do NOT change the outcome.

## Permission-denial heuristic (#45 → #61)

The existing `permission_denials_count` field stays load-bearing.
When `terminal_state == "completed"` AND `permission_denials_count
> 0` AND no `pr_linked` / `no_pr_declared` evidence is found, the
detector promotes the outcome to `incomplete_permission_denied` and
populates a `permission_denied` entry in `task_evidence` with the
denied tool names (sourced from the SDK's `permission_denials` list).

This generalizes the WARNING log line shipped in #45 — every
`incomplete_*` outcome MUST emit an operator-visible log entry per
SPEC §17.7.

## What `outcome_decided_by` tells you

| value | meaning |
|---|---|
| `detector` | The M5.2 evidence detector ran and found (or did not find) evidence in the world. The `task_outcome` reflects real observation. |
| `derivation` | The detector did not run; `task_outcome` was computed from existing provider fields per SPEC §17.4. Treat with mild suspicion when classifying many runs — derivation can mislabel a `completed_with_pr` run as `incomplete_no_evidence` because it has no way to check GitHub. |
| `unknown` | Neither path applied. Treat as needing investigation. |

For the M5.1 → M5.2 transition window, every new
`terminal.json` will have `outcome_decided_by: "derivation"` until
#60 ships. Operators auditing M5.1-window runs should rely on the
provider-side fields (`permission_denials_count`, `terminal_state`,
`blocked`) for now.

## Migration / backward compatibility

- Old `terminal.json` artifacts (no `task_outcome`) remain readable.
  Treat missing `task_outcome` as `"unknown"`; do not assert any
  positive outcome from absence.
- The new fields land in every artifact written after M5.1 ships.
  Operators looking back at pre-M5.1 runs see only the existing
  provider-outcome fields.
- The detector (M5.2) appends `task_evidence` entries; consumers
  MUST tolerate unknown `type` values in the array for forward
  compatibility — the schema may grow new evidence kinds (e.g.
  `tracker_handoff` for non-GitHub trackers, `mr_linked` for GitLab)
  without breaking existing parsers.

## What this design does NOT do

- It does NOT add a runtime status API or dashboard (covered by
  M6 #55 / #56).
- It does NOT add token / cost accounting (M6 #54).
- It does NOT change the orchestrator's claim/release semantics —
  whether the orchestrator releases the issue or marks it blocked
  is still driven by `terminal_state` + `retryable` + `blocked`,
  not by `task_outcome`. M5.3 (#62) will revisit whether
  `task_outcome` should influence retry/block routing.
- It does NOT define how Claude communicates a "no change needed"
  declaration in detail. The `marker_source` field has three allowed
  values, but the exact sentinel format (e.g.
  `Symphony-No-PR: <reason>`) is left to M5.2 to keep #61 design-only.

## Related work

- #45 — `permission_denials_count` field (foundation for the
  `incomplete_permission_denied` outcome).
- #51 — M5 parent / epic.
- #60 — M5.2 evidence detector implementation (consumes this schema).
- #62 — M5.3 classification of provider-completed-but-task-incomplete
  runs (uses `task_outcome` to drive retry / block routing).
- #63 — M5.4 artifacts/logs/runbook updates (operator surface for
  the new fields).
- #64 — M5.5 E2E re-run gating on the new outcome contract.
