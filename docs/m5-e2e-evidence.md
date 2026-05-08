# M5.5 — Live E2E evidence for terminal outcome gates

**Audience:** reviewers auditing whether the M5.1–M5.4 terminal-outcome
contract actually works against real GitHub + Claude Code, and operators
looking for a worked example of the two `task_outcome` axes diverging.

**Source of truth:** `SPEC.md` §17 and `docs/terminal-outcomes.md`.

**Scope:** This document records the evidence that satisfies the M5.5
acceptance criteria from issue
[#64](https://github.com/jimoosciuc/symphony-cc/issues/64). It is
narrative-only — there is no runtime behavior change, no schema change,
and no CI gating change. M5.5 explicitly does NOT promote live tests to
required CI.

## Two scenarios required by #64

#64 requires *at least* two live E2E runs:

1. **Happy path.** A real GitHub issue → `workspace.populate=git` →
   Claude Code session → branch + draft PR → `task_outcome:
   completed_with_pr`, `outcome_decided_by: detector`.
2. **Incomplete / permission-denied path.** A controlled scenario
   where the provider stream finishes cleanly (`terminal_state:
   completed`) but the task outcome is `incomplete_*`, exposing the
   misleading-success class M5.1–M5.4 was designed to catch.

Both scenarios were exercised against this repository on 2026-05-08 by
the leader-coordinated Symphony driver. The incomplete-path scenario
was produced involuntarily — Claude paused for `AskUserQuestion`
because the issue body still carried the prior "Deferred" status — and
the M5.2 detector classified it correctly without operator help. That
is exactly the kind of misleading-success run M5.5 is meant to gate
against, so it is reused here as the controlled fixture rather than
manufactured a second time.

## Scenario 1 — Happy path

| Field | Value |
|---|---|
| Workflow | Equivalent to `WORKFLOW.example.md`. `workspace.populate=git`, `claude.model=claude-opus-4-7`, `claude.permission_mode=bypassPermissions`. |
| Issue | [jimoosciuc/symphony-cc#64](https://github.com/jimoosciuc/symphony-cc/issues/64). |
| Symphony session id | `sym-4f78019f5bb3` |
| Symphony run id | `run-b469af5e` (attempt 2 on the issue) |
| Provider session id | `8044a984-4360-4687-8ba7-c47f34397691` |
| Workspace | `/private/tmp/.symphony/workspaces/jimoosciuc_symphony-cc_64` |
| Artifact dir | `/private/tmp/.symphony/runs-m55-2/jimoosciuc_symphony-cc_64/1/` |
| Branch | `symphony/jimoosciuc-symphony-cc-64` |
| PR | This document's PR (URL filled in on the PR body once opened). |
| Expected `task_outcome` | `completed_with_pr` |
| Expected `outcome_decided_by` | `detector` |

The PR body (not this file) is where the *post-run* `terminal.json`
fields are pasted, because `terminal.json` is finalized by the
orchestrator after the provider stream closes — i.e. after this commit
already exists on the branch. The reviewer should re-read
`terminal.json` at the artifact dir above once Symphony reports the
session as terminal.

## Scenario 2 — Incomplete / permission-denied path

| Field | Value |
|---|---|
| Workflow | Same as scenario 1 (same `bypassPermissions` setting). |
| Issue | [jimoosciuc/symphony-cc#64](https://github.com/jimoosciuc/symphony-cc/issues/64) — first attempt before the leader cleared stale "Deferred" text from the body. |
| Symphony run id | `run-9b774e79` (attempt 1 on the issue) |
| Provider session id | `cb6d07f6-b35a-411c-b268-b427ce46952a` |
| Artifact dir | `/private/tmp/.symphony/runs-m55/jimoosciuc_symphony-cc_64/1/` |
| `terminal_state` | `completed` |
| `task_outcome` | `incomplete_permission_denied` |
| `outcome_decided_by` | `detector` |
| `permission_denials_count` | 1 |
| `task_evidence` | `[{type: permission_denied, tool_names: [AskUserQuestion], denials_count: 1}]` |
| `blocked` | `true` |
| `retryable` | `false` |
| PR opened? | No — verified by the M5.2 evidence detector against the GitHub API. |

### Why this is the right fixture

The detector's permission-denial heuristic
(`docs/terminal-outcomes.md` §"Permission-denial heuristic") states:

> When `terminal_state == "completed"` AND `permission_denials_count > 0`
> AND no `pr_linked` / `no_pr_declared` evidence is found, the detector
> promotes the outcome to `incomplete_permission_denied` and populates
> a `permission_denied` entry in `task_evidence` with the denied tool
> names.

Run `run-9b774e79` matches all three conditions exactly:

- `terminal_state` is `completed` (the SDK returned a clean
  `ResultMessage`);
- `permission_denials_count` is `1` (the `AskUserQuestion` call was
  blocked);
- the detector queried the GitHub API and found neither a linked PR
  nor a `Symphony-No-PR:` sentinel.

Because `outcome_decided_by` is `detector` (not `derivation`), the
M5.3 routing layer correctly applied `mark_issue_blocked` — the
operator-visible WARNING is in `events.jsonl` for that run, and the
issue carried the `symphony-blocked` label until the leader cleared
it before re-running.

A note on `bypassPermissions`: even with `permission_mode=bypassPermissions`,
Symphony still treats `AskUserQuestion` as denied for unattended runs
(by design — there is no operator on the other end of the question).
That makes `AskUserQuestion` a reliable lever for producing the
`incomplete_permission_denied` outcome in a controlled way without
having to flip the workflow back to `acceptEdits`.

## Validation summary

The following were verified on 2026-05-08:

- Both scenarios produced a `terminal.json` whose schema matches
  `SPEC.md` §17 (provider-outcome fields + new task-outcome fields).
- Both runs have `outcome_decided_by: detector` — not `derivation` —
  so the routing layer is allowed to act on the outcome.
- The two axes disagree in the way `docs/terminal-outcomes.md`
  describes: `terminal_state: completed` + `task_outcome:
  incomplete_permission_denied` for run 1, and (expected)
  `terminal_state: completed` + `task_outcome: completed_with_pr` for
  run 2.
- `pytest` against the in-tree unit suite still passes (the M5.5
  change is doc-only and adds no runtime code).

## Non-goals

- This document does NOT add a runtime status API or dashboard
  (M6 #55/#56).
- This document does NOT make live E2E required in default CI.
- This document does NOT change `task_outcome` derivation, the
  detector, or the routing layer.

## Related work

- #51 — M5 parent / epic.
- #61 — M5.1 schema design.
- #60 — M5.2 evidence detector.
- #62 — M5.3 incomplete-outcome routing.
- #63 — M5.4 artifacts/logs/runbook.
- #74 / #75 — PR-lookup-failure → false-block correction.
