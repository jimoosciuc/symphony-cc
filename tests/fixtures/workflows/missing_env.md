---
tracker:
  kind: github
  owner: jimoosciuc
  repo: symphony-cc
  token: $SYMPHONY_TEST_NONEXISTENT_TOKEN

agent:
  provider: claude_code

workspace:
  root: ./.symphony/workspaces

claude:
  model: claude-sonnet-4-5
  permission_mode: acceptEdits
  session_store: ./.symphony/sessions
  transcript_store: ./.symphony/transcripts
  artifact_store: ./.symphony/runs

github: {}
---

You are working on {{ issue.identifier }}.
