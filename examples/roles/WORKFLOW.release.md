---
tracker:
  kind: github
  owner: your-org
  repo: your-repo
  token: $GITHUB_TOKEN
  include_labels: ["status:ready-to-merge"]
  exclude_labels: ["symphony-running", "do-not-claim"]

agent:
  provider: claude_code
  max_concurrency: 1
  max_turns: 2

workspace:
  root: .symphony/roles/release/workspaces
  populate: git

github:
  claim_label: symphony-running
  ready_label: status:ready-to-merge
  blocked_label: status:blocked
  done_label: status:done
  branch_prefix: release
  base_branch: main
  draft_pr: true
  claim_comment: true
  pr_link_comment: true

security:
  profile: conservative

claude:
  model: claude-opus-4-7
  permission_mode: acceptEdits
  session_store: .symphony/roles/release/sessions
  transcript_store: .symphony/roles/release/transcripts
  artifact_store: .symphony/roles/release/runs
---

You are the release role.

Issue: {{ issue.identifier }} - {{ issue.title }}
URL: {{ issue.url }}

Prepare the linked PR for merge:
- verify required checks are green;
- confirm reviewer approval and verifier evidence exist;
- confirm the PR is still limited to the issue scope;
- merge only when the repository policy allows it;
- close or label the issue after merge.

Do not make unrelated code changes. If merge readiness is unclear, comment with
the blocker and hand back to the leader.
