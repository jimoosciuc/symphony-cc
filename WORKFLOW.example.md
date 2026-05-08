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
  # Optional cleanup policy (M5.6). Defaults are off — workspaces are
  # preserved across runs (SPEC §8). Uncomment + enable to opt in.
  # The executor lands in M5.7 (#66); until then `enabled: true` is
  # accepted but has no effect at runtime.
  # cleanup:
  #   enabled: false
  #   on_terminal_issue: false   # delete after issue closes/done
  #   on_closed_pr: false        # delete after the linked PR closes/merges
  #   max_age_days: null         # delete workspaces older than N days
  #   dry_run: false             # log intent without deleting

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
  # Optional artifact retention. Defaults are off — artifacts are audit
  # evidence and are kept forever unless the operator opts in. Only
  # age-based retention is supported (no terminal/PR-close triggers).
  # When enabled, Symphony writes retention reports under
  # <artifact_store>/_retention_reports/.
  # artifact_retention:
  #   enabled: false
  #   max_age_days: null
  #   dry_run: false
---

You are working unattended on {{ issue.identifier }}: {{ issue.title }}.

Issue URL: {{ issue.url }}
Current state: {{ issue.state }}

Read the repository, make the smallest correct change, run focused validation,
and open or update a pull request linked to this issue. Leave a concise summary
of what changed and what was verified.
