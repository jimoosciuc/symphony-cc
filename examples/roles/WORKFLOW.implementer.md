---
tracker:
  kind: github
  owner: your-org
  repo: your-repo
  token: $GITHUB_TOKEN
  include_labels: ["status:ready-for-implementation"]
  exclude_labels: ["symphony-running", "do-not-claim", "leader-owned"]

agent:
  provider: claude_code
  max_concurrency: 1
  max_turns: 5

workspace:
  root: .symphony/roles/implementer/workspaces
  populate: git

github:
  claim_label: symphony-running
  ready_label: status:ready-for-implementation
  blocked_label: status:blocked
  done_label: status:ready-for-review
  branch_prefix: implementer
  base_branch: main
  draft_pr: true
  claim_comment: true
  pr_link_comment: true

security:
  profile: trusted_unattended

claude:
  model: claude-opus-4-7
  permission_mode: bypassPermissions
  session_store: .symphony/roles/implementer/sessions
  transcript_store: .symphony/roles/implementer/transcripts
  artifact_store: .symphony/roles/implementer/runs
---

You are the implementer role.

Issue: {{ issue.identifier }} - {{ issue.title }}
URL: {{ issue.url }}

Implement the smallest correct change that satisfies the issue:
- read the issue, linked PRs, and recent comments before editing;
- keep changes limited to the requested scope;
- add or update tests proportional to the risk;
- run focused validation and then `make ci` when practical;
- open or update a draft PR linked to this issue.

Do not claim issues labeled `leader-owned` or `do-not-claim`. Do not review or
merge your own PR. Hand off to `status:ready-for-review` with a concise summary
and validation evidence.
