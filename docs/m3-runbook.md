# M3 — Local end-to-end run

## What this is

The M3 milestone proves Symphony can drive a real GitHub issue through
Claude Code and produce a real pull request. It is not a unit test; it
needs real credentials and a real Claude CLI session.

This document is the runbook. Follow each step on a workstation with
network access to GitHub and a working `claude` CLI login. The result
is the **evidence bundle** at the bottom — file it as a comment on
issue #11 (or as `docs/m3-evidence.md` if you prefer to keep it in
repo).

## Prerequisites

- `python >= 3.10`, `pip install -e ".[dev]"` from the repo root.
- `claude` CLI on `PATH` and authenticated (`claude --version` should
  print).
- A GitHub personal access token with at least `repo` scope, exported
  as `GITHUB_TOKEN`.
- A target GitHub repository where you have permission to create
  branches and pull requests. The leader's `jimoosciuc/symphony-cc`
  works; a private throwaway repo also works.
- A low-risk issue in that repository. Apply the
  `symphony-ready` label so Symphony's tracker filter picks it up
  (configurable via `tracker.include_labels`).

## Setup

1. **Create or pick a target issue.** Suggested first run: a one-line
   doc tweak ("Fix typo in README intro"). Apply `symphony-ready`.

2. **Make a workflow file.** Start from `WORKFLOW.example.md`:

   ```bash
   cp WORKFLOW.example.md /tmp/m3-workflow.md
   $EDITOR /tmp/m3-workflow.md
   ```

   Edit the `tracker:` block to point at your repo:

   ```yaml
   tracker:
     kind: github
     owner: <your-gh-handle>
     repo: <your-test-repo>
     token: $GITHUB_TOKEN
     include_labels: ["symphony-ready"]
     exclude_labels: ["symphony-running", "symphony-blocked"]
   ```

   Confirm `claude.session_store`, `claude.transcript_store`,
   `claude.artifact_store` resolve under a writable directory (the
   defaults `.symphony/...` resolve relative to the workflow file).

3. **Export the token** (in the shell that will run Symphony):

   ```bash
   export GITHUB_TOKEN=ghp_xxxxxxxxxxxxxxxxxxxxxxxx
   ```

4. **Sanity-check the workflow loads** without running anything:

   ```bash
   python -c "from symphony.workflow import load_workflow; w = load_workflow('/tmp/m3-workflow.md'); print(w.config.tracker.owner)"
   ```

## Run

Single-tick mode (recommended for the first run):

```bash
symphony run --workflow /tmp/m3-workflow.md --once --log-level debug
```

What happens:

1. CLI loads the workflow, instantiates `GitHubTracker`,
   `ClaudeCodeProvider`, `WorkspaceManager`, `Orchestrator`.
2. Orchestrator polls candidate issues from your repo.
3. The first eligible issue is claimed (`symphony-running` label
   added; if `claim_comment: true`, a comment is posted).
4. A workspace is created at
   `<workspace.root>/<owner>_<repo>_<issue_number>/`. When
   `workspace.populate: git` is set (the default in
   `WORKFLOW.example.md`), Symphony clones the tracker repo into the
   workspace from `github.base_branch` on first run, and on reuse
   refreshes via `git fetch` + `git reset --hard origin/<base_branch>`
   + `git clean -fdx`. Reuse is destructive of any uncommitted local
   state by design — every dispatch starts from a deterministic
   checkout. The `tracker.token` is used for clone auth and is NEVER
   persisted into `.git/config`.
5. `ClaudeCodeProvider.start_session` connects to the local Claude
   CLI; `send_input` drives one turn with the rendered first prompt.
6. Claude's session ID is captured into
   `<claude.session_store>/<session_id>.json` after the first event.
7. Per-attempt artifacts land at
   `<claude.artifact_store>/<owner>_<repo>_<n>/1/` (events.jsonl,
   request.json, session.json, terminal.json).
8. The orchestrator releases the claim and prints a tick summary.

If you want long-running mode, drop `--once`. The orchestrator will
poll forever at `polling.interval_ms`.

## Stopping the daemon

`Ctrl-C` (SIGINT). The CLI catches it, releases trackers, and exits
130. In-flight workers are NOT cancelled — they finish their current
turn first. Aggressive shutdown semantics are deferred to a follow-up.

## Evidence to capture

Fill in this template and post as a comment on
[issue #11](https://github.com/jimoosciuc/symphony-cc/issues/11):

```markdown
## M3 E2E evidence

- **Workflow file**: `/tmp/m3-workflow.md` (sha: `git hash-object …`)
- **GitHub issue**: <issue url>
- **Branch name**: `symphony/<owner>-<repo>-<issue_number>`
- **PR URL**: <pr url> (or "no PR — see Limitations")
- **Artifact directory**: <absolute path>
- **Session record**:
  - session_id: `sym-…`
  - provider_session_id: `<claude session uuid>`
  - persisted at: `<session_store>/<session_id>.json`
- **Transcript location**: `~/.claude/projects/<encoded-cwd>/<session_id>.jsonl`
  (best-effort; CLI-managed)
- **Validation commands run**:
  - `cat <artifact_dir>/terminal.json`
  - `pytest -q` (if the issue's change is testable)
- **Tick summary** (from `symphony run --once` stdout):
  ```
  symphony tick result:
    dispatched: ['…']
    finished: ['…']
    reconciled_cancelled: []
    skipped_claim_conflict: []
    retries_scheduled: []
  ```
- **Known limitations / follow-ups**:
  - <e.g. "PR coordinator is agent-managed; if Claude doesn't push, no PR is created.">
  - <e.g. "long-running mode doesn't cancel workers on Ctrl-C; tracked at #….">
```

## Troubleshooting

- **`workflow load failed: tracker.token: environment variable
  $GITHUB_TOKEN is not set`** — export the token in the same shell.
- **`claude-agent-sdk is not installed`** — `pip install -e .` (the
  SDK is a runtime dep).
- **Claim conflicts (`skipped_claim_conflict` non-empty)** — another
  Symphony run already claimed the issue. Manually remove the
  `symphony-running` label or wait for the other run to release.
- **Run looks successful but no commits / no PR landed** — check
  `terminal.json:permission_denials_count`. A non-zero count means
  Claude was denied tool calls (typically `Bash` or
  `AskUserQuestion`) under `permission_mode: acceptEdits`. Stock
  unattended PR work needs `git`/`gh`/shell, so `acceptEdits` cannot
  complete it — switch the workflow to `permission_mode:
  bypassPermissions` (only on trusted hosts) and re-run. The
  orchestrator also emits a WARNING log line when this happens, so
  the signal shows up in `--log-level info` (the default) without
  parsing artifacts.
- **`session.json` shows `provider_session_id: null` after a turn** —
  Claude didn't return a `session_id` on the first message. Check
  `<artifact_dir>/events.jsonl` for the raw event sequence; file an
  issue against the provider if it reproduces.
- **Workspace directory survives across runs** — by design (SPEC §8).
  Delete manually if you want a fresh checkout.

## What's NOT covered by this runbook

- Multi-issue concurrency (`agent.max_concurrency > 1`). Works in
  unit tests; hasn't been exercised end-to-end.
- `github_graphql` tool exposure to Claude (M4 #13).

## Restart recovery

When the daemon is killed mid-flight (Ctrl-C, crash, OOM), Symphony
preserves enough state to reconcile on the next `symphony run`:

- The workspace under `workspace.root/<owner>_<repo>_<n>/` is reused.
- The session record at `claude.session_store/<session_id>.json` is
  inspected (records with `terminal_state` already set are skipped).
- The per-attempt artifact dir under `claude.artifact_store/.../<n>/`
  records what happened.

On startup, before the first poll tick, the orchestrator runs a
`recover()` pass that — for each in-flight session record — fetches
the fresh issue state and routes per `claude.retry_resume_policy`:

- **`resume_same_session`** (default): calls `provider.restore()` and
  drives the resumed worker to a terminal state. If `restore()` fails
  (no `provider_session_id`, SDK error), the issue is marked
  `symphony-blocked`.
- **`new_session_with_summary`**: releases the prior claim and stashes
  the prior `provider_session_id` chain so the next dispatch tick
  produces a fresh session whose continuation prompt can reference the
  prior conversation.
- **`fail_closed`**: marks the issue blocked unconditionally — operator
  must clear `symphony-blocked` before the daemon retries.

Issues that became ineligible while the daemon was down (closed, hit an
exclude label, picked up `symphony-blocked` manually) are released
without a resume attempt. Issues the tracker no longer knows about
(deleted, transferred) are discarded.

Every recovery decision is written to
`claude.artifact_store/<owner>_<repo>_<n>/<attempt>/recovery.json` and
echoed to the CLI's `--once` output:

```text
symphony recovery decisions:
  resumed acme/proj#5 (session=sym-abc123): restored via provider.restore()
  released acme/proj#7: issue not eligible: issue is 'closed'
  blocked acme/proj#9: provider restore failed: ...
```

The on-disk session record is stamped terminal after each decision so a
second `recover()` call is a no-op.
