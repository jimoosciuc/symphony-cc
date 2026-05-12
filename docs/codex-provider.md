# Codex Provider

Symphony can run GitHub issues with Codex by setting `agent.provider:
codex` and using a top-level `codex:` runtime section.

## Generate A Workflow

```bash
symphony init github-implementer \
  --provider codex \
  --repo OWNER/REPO \
  --output WORKFLOW.md
```

For role workflows:

```bash
symphony init github-human-review \
  --provider codex \
  --repo OWNER/REPO \
  --output WORKFLOW.md
```

`symphony init` does not overwrite an existing workflow unless `--force`
is passed.

## Minimal Workflow

```yaml
---
tracker:
  kind: github
  owner: OWNER
  repo: REPO
  token: $GITHUB_TOKEN
  include_labels: ["symphony-ready"]
  exclude_labels: ["symphony-running", "symphony-blocked", "symphony-done"]

agent:
  provider: codex
  max_concurrency: 1
  max_turns: 3

workspace:
  root: .symphony/workspaces
  populate: git

github:
  ready_label: symphony-ready
  claim_label: symphony-running
  blocked_label: symphony-blocked
  done_label: symphony-done
  branch_prefix: symphony
  base_branch: main
  draft_pr: true
  claim_comment: true
  pr_link_comment: true
  close_issue_on_done: false

security:
  profile: trusted_unattended

codex:
  model: gpt-5.3-codex
  permission_mode: bypassPermissions
  session_store: .symphony/sessions
  transcript_store: .symphony/transcripts
  artifact_store: .symphony/runs
  turn_timeout_ms: 3600000
  stall_timeout_ms: 300000
  retry_resume_policy: resume_same_session
---
You are the Symphony implementer for {{ issue.identifier }}.

Work on exactly this issue. Open or update one pull request when code
changes are needed. If design approval is required, comment a concrete
proposal on the issue and stop with `Symphony-No-PR: design proposed`.
```

## Runtime Behavior

The Codex provider shells out to the local `codex` CLI. It sends the
rendered prompt through stdin, reads `codex exec --json` JSONL events,
and stores Codex's `thread_id` as Symphony's `provider_session_id`.
Later turns resume with `codex exec resume`.

Normal runs write provider artifacts under `codex.artifact_store`,
including:

- normalized Symphony events through the usual run artifacts
- `codex-events.jsonl` with the raw Codex JSONL stream
- `codex-stderr.txt` with full stderr diagnostics when stderr is present
- `codex-last-message.txt` with Codex's final assistant message

## Permission And Sandbox Mapping

For `permission_mode: bypassPermissions`, Symphony invokes Codex with
`--dangerously-bypass-approvals-and-sandbox`. Use this only on trusted
hosts, trusted repositories, and trusted issues.

For other permission modes, Symphony uses unattended approvals with a
workspace-write sandbox. If Codex reports that a requested action was
blocked, read-only, or permission denied, Symphony surfaces that signal
as `permission_denials` on the terminal event so dashboard and
`terminal.json` evidence do not look like a clean success.

## Migration From Early Codex Workflows

Early Codex support accepted `agent.provider: codex` with runtime
settings under `claude:`. That still loads for compatibility and emits a
config warning. New workflows should move those fields under `codex:`.

## Live Smoke Test

Live Codex tests are opt-in:

```bash
SYMPHONY_RUN_CODEX_INTEGRATION=1 \
PYTHONPATH=src \
pytest -m codex_live tests/test_codex_provider_live.py -q
```

Optional overrides:

- `SYMPHONY_CODEX_BIN=/path/to/codex`
- `SYMPHONY_CODEX_TEST_MODEL=gpt-5.3-codex`
- `SYMPHONY_CODEX_TEST_PERMISSION_MODE=acceptEdits`
