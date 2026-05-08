---
tracker:
  kind: github
  owner: your-org
  repo: your-repo
  token: $GITHUB_TOKEN
  include_labels: ["status:ready-for-verification"]
  exclude_labels: ["symphony-running", "do-not-claim"]

agent:
  provider: claude_code
  max_concurrency: 1
  max_turns: 2

workspace:
  root: .symphony/roles/verifier/workspaces
  populate: git

github:
  claim_label: symphony-running
  ready_label: status:ready-for-verification
  blocked_label: status:blocked
  done_label: status:ready-to-merge
  branch_prefix: verifier
  base_branch: main
  draft_pr: true
  claim_comment: true
  pr_link_comment: true

security:
  profile: conservative

claude:
  model: claude-opus-4-7
  permission_mode: acceptEdits
  session_store: .symphony/roles/verifier/sessions
  transcript_store: .symphony/roles/verifier/transcripts
  artifact_store: .symphony/roles/verifier/runs
---

You are the verifier role.

Issue: {{ issue.identifier }} - {{ issue.title }}
URL: {{ issue.url }}

Verify the linked PR against the issue acceptance criteria:
- inspect CI status and reviewer comments;
- run focused tests locally when needed;
- confirm docs were updated for behavior changes;
- post an evidence comment with commands and results.

If verification passes, recommend `status:ready-to-merge`. If it fails, explain
the failure and recommend `status:test-failed`.
