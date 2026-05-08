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
  permission_mode: acceptEdits
  session_store: .symphony/sessions
  transcript_store: .symphony/transcripts
  artifact_store: .symphony/runs
  turn_timeout_ms: 3600000
  stall_timeout_ms: 300000
  retry_resume_policy: resume_same_session
---

You are working unattended on {{ issue.identifier }}: {{ issue.title }}.

Issue URL: {{ issue.url }}
Current state: {{ issue.state }}

Read the repository, make the smallest correct change, run focused validation,
and open or update a pull request linked to this issue. Leave a concise summary
of what changed and what was verified.
