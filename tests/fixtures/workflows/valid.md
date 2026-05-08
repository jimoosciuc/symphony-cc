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
  max_concurrency: 2
  max_turns: 5

workspace:
  root: ./.symphony/workspaces

claude:
  model: claude-opus-4-7
  permission_mode: acceptEdits
  session_store: ./.symphony/sessions
  transcript_store: ./.symphony/transcripts
  artifact_store: ./.symphony/runs

github:
  base_branch: main
  draft_pr: true
---

You are working unattended on {{ issue.identifier }}: {{ issue.title }}.

URL: {{ issue.url }}
State: {{ issue.state }}
