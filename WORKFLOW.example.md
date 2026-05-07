---
tracker:
  kind: linear
  project_slug: symphony
  api_key: $LINEAR_API_KEY

agent:
  provider: claude_code
  max_concurrency: 1
  max_turns: 3

workspace:
  root: .symphony/workspaces
  before_run: null
  after_run: null

claude:
  model: claude-sonnet-4-5
  permission_mode: acceptEdits
  session_store: .symphony/sessions
  transcript_store: .symphony/transcripts
  artifact_store: .symphony/runs
  turn_timeout_ms: 3600000
  stall_timeout_ms: 300000
  retry_resume_policy: resume_same_session
---

You are working unattended on {{ issue.identifier }}: {{ issue.title }}.

Issue URL: {{ issue.url }}
Current state: {{ issue.state }}

Read the repository, make the smallest correct change, run focused validation,
and leave a concise summary of what changed and what was verified.

