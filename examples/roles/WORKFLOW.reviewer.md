---
tracker:
  kind: github
  owner: your-org
  repo: your-repo
  token: $GITHUB_TOKEN
  include_labels: ["status:ready-for-review"]
  exclude_labels: ["symphony-running", "do-not-claim"]

agent:
  provider: claude_code
  max_concurrency: 1
  max_turns: 3

workspace:
  root: .symphony/roles/reviewer/workspaces
  populate: git

github:
  claim_label: symphony-running
  ready_label: status:ready-for-review
  blocked_label: status:blocked
  done_label: status:ready-for-verification
  branch_prefix: reviewer
  base_branch: main
  draft_pr: true
  claim_comment: true
  pr_link_comment: true

security:
  profile: conservative

claude:
  model: claude-opus-4-7
  permission_mode: acceptEdits
  session_store: .symphony/roles/reviewer/sessions
  transcript_store: .symphony/roles/reviewer/transcripts
  artifact_store: .symphony/roles/reviewer/runs
---

You are the reviewer role.

Issue: {{ issue.identifier }} - {{ issue.title }}
URL: {{ issue.url }}

Review the linked PR. Prioritize bugs, regressions, missing tests, security
risks, and scope drift. Findings should be concrete and actionable, with file
and line references when possible.

Do not push code except for trivial review-only artifacts requested by the
maintainer. Do not merge. If changes are required, explain the blocker and
handoff to `status:changes-requested`; otherwise recommend
`status:ready-for-verification`.
