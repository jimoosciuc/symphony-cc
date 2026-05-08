---
tracker:
  kind: github
  owner: your-org
  repo: your-repo
  token: $GITHUB_TOKEN
  include_labels: ["status:changes-requested", "status:test-failed"]
  exclude_labels: ["symphony-running", "do-not-claim", "leader-owned"]

agent:
  provider: claude_code
  max_concurrency: 1
  max_turns: 4

workspace:
  root: .symphony/roles/fixer/workspaces
  populate: git

github:
  claim_label: symphony-running
  ready_label: status:changes-requested
  blocked_label: status:blocked
  done_label: status:ready-for-review
  branch_prefix: fixer
  base_branch: main
  draft_pr: true
  claim_comment: true
  pr_link_comment: true

security:
  profile: trusted_unattended

claude:
  model: claude-opus-4-7
  permission_mode: bypassPermissions
  session_store: .symphony/roles/fixer/sessions
  transcript_store: .symphony/roles/fixer/transcripts
  artifact_store: .symphony/roles/fixer/runs
---

You are the fixer role.

Issue: {{ issue.identifier }} - {{ issue.title }}
URL: {{ issue.url }}

Address reviewer-requested changes or verifier failures:
- read the review comments and failing checks first;
- keep the fix limited to the requested correction;
- add a regression test when the failure describes a bug;
- update the existing PR rather than opening unrelated work.

When fixed, hand off to `status:ready-for-review` with validation evidence.
