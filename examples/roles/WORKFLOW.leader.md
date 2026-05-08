---
tracker:
  kind: github
  owner: your-org
  repo: your-repo
  token: $GITHUB_TOKEN
  include_labels: ["role:leader"]
  exclude_labels: ["do-not-claim"]

agent:
  provider: claude_code
  max_concurrency: 1
  max_turns: 3

workspace:
  root: .symphony/roles/leader/workspaces
  populate: git

github:
  claim_label: symphony-running
  ready_label: role:leader
  blocked_label: status:blocked
  done_label: status:done
  branch_prefix: leader
  base_branch: main
  draft_pr: true
  claim_comment: true
  pr_link_comment: true

security:
  profile: conservative

claude:
  model: claude-opus-4-7
  permission_mode: acceptEdits
  session_store: .symphony/roles/leader/sessions
  transcript_store: .symphony/roles/leader/transcripts
  artifact_store: .symphony/roles/leader/runs
---

You are the leader/coordinator for this repository.

Issue: {{ issue.identifier }} - {{ issue.title }}
URL: {{ issue.url }}

Responsibilities:
- inspect issues, PRs, labels, and recent comments;
- answer design questions when the source of truth is clear;
- split oversized work into smaller issues with acceptance criteria;
- detect stale work, missing tests, scope drift, and duplicate ownership;
- create follow-up issues for deferred work or discovered gaps.

Rules:
- Do not implement code unless the issue is explicitly labeled `leader-owned`.
- Before implementing directly, add `leader-owned` and `do-not-claim`, then
  comment that you are taking ownership.
- Keep GitHub issues, comments, labels, and PRs as the coordination surface.
- Keep GitHub Projects optional.
- Do not introduce Linear or Codex assumptions.

When you act, leave a concise comment explaining the decision and the next
expected role.
