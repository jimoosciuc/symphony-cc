---
tracker:
  kind: github
  owner: your-org
  repo: your-repo
  token: $GITHUB_TOKEN
  include_labels: ["status:needs-spec"]
  exclude_labels: ["symphony-running", "do-not-claim", "leader-owned"]

agent:
  provider: claude_code
  max_concurrency: 1
  max_turns: 2

workspace:
  root: .symphony/roles/planner/workspaces
  populate: git

github:
  claim_label: symphony-running
  ready_label: status:needs-spec
  blocked_label: status:blocked
  done_label: status:ready-for-implementation
  branch_prefix: planner
  base_branch: main
  draft_pr: true
  claim_comment: true
  pr_link_comment: true

security:
  profile: conservative

claude:
  model: claude-opus-4-7
  permission_mode: acceptEdits
  session_store: .symphony/roles/planner/sessions
  transcript_store: .symphony/roles/planner/transcripts
  artifact_store: .symphony/roles/planner/runs
---

You are the planner role.

Issue: {{ issue.identifier }} - {{ issue.title }}
URL: {{ issue.url }}

Turn the request into an implementation-ready ticket:
- clarify the goal and non-goals;
- identify dependencies and likely files/modules;
- define acceptance criteria and focused validation;
- recommend whether to split the issue before implementation.

Prefer comments and issue-body suggestions over code changes. Do not implement
runtime code. When the issue is ready, explain the proposed label transition to
`status:ready-for-implementation`.
