# Role-Specific Workflow Examples

Symphony can run a production-line model by starting multiple `symphony run`
processes, each with a different workflow file and a different issue label
filter. This does not require a new scheduler. GitHub issues, labels, comments,
and PRs remain the required coordination surface.

GitHub Projects can be useful for visibility, but they are optional metadata.
Do not make project membership a prerequisite for a role daemon to work.

## Roles

| Role | Primary input | Primary output |
| --- | --- | --- |
| Leader | `role:leader`, `status:blocked`, stale PRs/issues | Decisions, split issues, follow-ups, `leader-owned` work |
| Planner | `status:needs-spec` | Implementation-ready issue body and acceptance criteria |
| Implementer | `status:ready-for-implementation` | Draft or ready PR, `status:ready-for-review` |
| Reviewer | `status:ready-for-review` | Review comments, `status:changes-requested` or `status:ready-for-verification` |
| Fixer | `status:changes-requested`, `status:test-failed` | PR updates, `status:ready-for-review` |
| Verifier | `status:ready-for-verification` | Evidence comment, `status:ready-to-merge` or `status:test-failed` |
| Release | `status:ready-to-merge` | Merged PR, closed issue |

The examples use `role:*` and `status:*` labels because they are explicit and
easy to route. Repositories can map these to existing labels, but every role
should have a narrow input label set.

## Ownership Rules

- A role must only claim issues matching its input labels.
- A role must inspect recent issue comments, linked PRs, and labels before
  starting work.
- A role must not claim issues labeled `do-not-claim`.
- A role must not claim issues labeled `leader-owned` unless it is the leader.
- A role should post a short claim/status comment when it starts work.
- A role should remove its input label and add the next status label when it
  hands off work.
- If a task is too broad, the leader or planner should split it before
  implementation starts.

Leader direct implementation has an extra rule: the leader must first mark the
issue `leader-owned` and `do-not-claim`, then comment that the leader is taking
the work. This prevents an implementer from starting the same issue.

## Permission Model

Use separate GitHub tokens when possible.

| Role | Suggested permission shape |
| --- | --- |
| Planner | Issues read/write for comments and labels. Contents read. |
| Reviewer | Pull requests read/write for review comments. Contents read. Issues read/write for status labels. |
| Implementer | Contents write, pull requests write, issues read/write. No merge permission. |
| Fixer | Same as implementer. |
| Verifier | Contents read, pull requests read, issues read/write for evidence/status labels. |
| Release | Pull requests write/merge and issues write/close. Avoid broad code editing duties. |
| Leader | Issues/PRs read/write. Code write only when explicitly using `leader-owned`. |

For small teams, the same token can run multiple roles, but the workflow prompt
should still state the role boundary clearly.

## Label Handoff

Recommended lifecycle:

```text
status:needs-spec
  -> planner
  -> status:ready-for-implementation
  -> implementer
  -> status:ready-for-review
  -> reviewer
  -> status:changes-requested
  -> fixer
  -> status:ready-for-review
```

or, after approval:

```text
status:ready-for-review
  -> reviewer
  -> status:ready-for-verification
  -> verifier
  -> status:ready-to-merge
  -> release
  -> closed
```

Blocked work:

```text
status:blocked
  -> leader
  -> status:ready-for-implementation
```

Use comments to explain each transition. A label change without a handoff
comment is hard for the next role to trust.

## Running Roles

Copy the example workflows and edit owner, repo, paths, and model:

```bash
cp -R examples/roles /tmp/symphony-roles
$EDITOR /tmp/symphony-roles/WORKFLOW.implementer.md
```

Run one daemon per role:

```bash
symphony run --workflow /tmp/symphony-roles/WORKFLOW.leader.md
symphony run --workflow /tmp/symphony-roles/WORKFLOW.implementer.md
symphony run --workflow /tmp/symphony-roles/WORKFLOW.reviewer.md
```

Use separate workspace/session/artifact roots for each role. This avoids
artifact overlap and makes it clear which role produced a PR or comment.

For smaller deployments, one daemon can also define runtime lanes in a single
workflow file. Lanes keep the same GitHub-first label handoff model but let the
orchestrator select a role profile per issue:

```yaml
lanes:
  - name: implementer
    include_labels: ["status:ready-for-implementation"]
    exclude_labels: ["do-not-claim", "leader-owned"]
    max_concurrency: 2
    prompt_prefix: "You are the implementer. Make scoped code changes."
    prompt_suffix: "When complete, open a PR linked to the issue."
  - name: reviewer
    include_labels: ["status:ready-for-review"]
    exclude_labels: ["do-not-claim", "leader-owned"]
    max_concurrency: 1
    prompt_prefix: "You are the reviewer. Review the linked PR only."
  - name: leader
    include_labels: ["status:blocked"]
    exclude_labels: ["do-not-claim"]
    max_concurrency: 1
    prompt_prefix: "You are the leader. Clarify, split, or unblock work."
```

Runtime lanes are optional. Existing single-role workflow files still work.
Use lane-level prompts for role boundaries; use GitHub labels and comments for
handoff. GitHub Projects remain optional metadata and must not be required for
lane selection.

## Example Files

The repository includes copyable workflow examples:

- `examples/roles/WORKFLOW.leader.md`
- `examples/roles/WORKFLOW.planner.md`
- `examples/roles/WORKFLOW.implementer.md`
- `examples/roles/WORKFLOW.reviewer.md`
- `examples/roles/WORKFLOW.fixer.md`
- `examples/roles/WORKFLOW.verifier.md`
- `examples/roles/WORKFLOW.release.md`

They are examples, not a hard protocol. The invariant is that every role has a
clear input label set, a clear output handoff, and a prompt that tells it not to
duplicate another role's work.

## Non-Goals

This role model does not add:

- a database,
- GitHub Projects as a requirement,
- Linear support,
- Codex support,
- automatic merge authority for non-release roles.

Those can be designed later if the simple multi-process or runtime-lane model
proves useful.
