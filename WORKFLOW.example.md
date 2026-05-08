---
tracker:
  kind: github
  owner: jimoosciuc
  repo: symphony-cc
  token: $GITHUB_TOKEN
  include_labels: ["symphony-ready"]
  exclude_labels: ["symphony-running", "symphony-blocked"]

agent:
  provider: claude_code
  max_concurrency: 1
  max_turns: 3

workspace:
  root: .symphony/workspaces
  populate: git
  before_run: null
  after_run: null

github:
  claim_label: symphony-running
  ready_label: symphony-ready
  blocked_label: symphony-blocked
  done_label: symphony-done
  branch_prefix: symphony
  base_branch: main
  draft_pr: true
  claim_comment: true
  pr_link_comment: true

claude:
  model: claude-opus-4-7
  # `acceptEdits` lets Claude write files but blocks Bash, AskUserQuestion,
  # and other interactive tools. Stock unattended PR work that needs `git`,
  # `gh`, or shell commands therefore CANNOT complete under `acceptEdits` —
  # the run will look successful in tracker output but no commits or PR
  # will land. Inspect `terminal.json:permission_denials_count` after a
  # run; non-zero means Claude was bounced. Use `bypassPermissions` for
  # truly unattended runs (only on trusted hosts) or keep `acceptEdits`
  # for human-in-the-loop sessions where you'll grant tool calls.
  permission_mode: acceptEdits
  session_store: .symphony/sessions
  transcript_store: .symphony/transcripts
  artifact_store: .symphony/runs
  turn_timeout_ms: 3600000
  stall_timeout_ms: 300000
  retry_resume_policy: resume_same_session

# Workspace cleanup policy (#65 schema; #66 executor; #67 reporting).
# Default-safe: disabled. Existing workflows that omit `cleanup` keep
# preserving workspaces exactly as before. When `enabled: true`, at
# least one of `on_terminal_issue`, `on_closed_pr`, or `max_age_days`
# MUST be set or workflow loading fails. `dry_run: true` makes the
# future executor list candidates without deleting them.
cleanup:
  enabled: false
  on_terminal_issue: false
  on_closed_pr: false
  max_age_days: null
  dry_run: false

# Artifact retention policy. Configured separately from `cleanup`
# because run artifacts under `claude.artifact_store` are audit
# evidence. Default-safe: disabled. When `enabled: true`,
# `artifact_max_age_days` MUST be set; refusing to delete artifacts
# without an explicit bound is intentional.
retention:
  enabled: false
  artifact_max_age_days: null
  dry_run: false
---

You are working unattended on {{ issue.identifier }}: {{ issue.title }}.

Issue URL: {{ issue.url }}
Current state: {{ issue.state }}

Read the repository, make the smallest correct change, run focused validation,
and open or update a pull request linked to this issue. Leave a concise summary
of what changed and what was verified.
