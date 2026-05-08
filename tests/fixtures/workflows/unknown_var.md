---
tracker:
  kind: github
  owner: jimoosciuc
  repo: symphony-cc
  token: $GITHUB_TOKEN

agent:
  provider: claude_code

workspace:
  root: ./.symphony/workspaces

claude:
  model: claude-opus-4-7
  permission_mode: acceptEdits
  session_store: ./.symphony/sessions
  transcript_store: ./.symphony/transcripts
  artifact_store: ./.symphony/runs

github: {}
---

Hello {{ issue.identifier }}, also referencing {{ unknown_thing }}.
