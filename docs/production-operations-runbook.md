# Production Operations Runbook

This runbook describes how to operate Symphony as a trusted service. It assumes
the Claude Code first, GitHub first implementation in this repository and does
not apply to Linear or Codex deployments.

Use this document after `docs/production-readiness.md` has identified which
validation level is required for the deployment.

## Supported Pilot Shape

The recommended first production pilot is:

- one trusted coordinator host;
- one target GitHub repository;
- one GitHub token scoped to that repository;
- authenticated Claude Code on the coordinator host;
- local workspaces and artifacts on durable disk;
- `symphony run --workflow <workflow> --dashboard` under a process supervisor;
- optional remote workers only after `make live-remote-claude` passes.

Do not use remote workers, runtime lanes, or `agent.max_concurrency > 1` in
production until their matching live validation targets have passed for the
same repository and credential class.

## Required Accounts And Secrets

| Secret | Required for | Minimum guidance |
| --- | --- | --- |
| `GITHUB_TOKEN` | GitHub issue polling, claim/release labels, branch push, PR evidence lookup. | Fine-grained PAT for the target repo with metadata read, issues read/write, contents read/write, and pull requests read/write. |
| Claude CLI auth | Local Claude provider. | Authenticate `claude` for the OS user that runs the service. Do not rely on an interactive shell profile. |
| `SYMPHONY_REMOTE_GIT_TOKEN` | Remote worker `workspace.populate: git`. | Git-only credential scoped to repository contents. Do not use the tracker token as the remote git credential unless the remote host is trusted to the same level as the coordinator. |
| `ANTHROPIC_*` env vars | Optional live Claude workflow in GitHub Actions. | Store in GitHub Actions secrets or environment variables only when the manual live workflow needs them. |

Never put token literals in `WORKFLOW.md`. Use `$ENV_VAR` references and load
the environment through the supervisor.

## Host Prerequisites

Coordinator host:

- Python `>=3.10`;
- repository checkout for `symphony-cc`;
- `pip install -e ".[dev]"` or an installed wheel that provides `symphony`;
- `git` and `gh` if workflows expect Claude to create PRs through shell tools;
- authenticated `claude` CLI on `PATH`;
- writable directories for workspaces, sessions, transcripts, artifacts, and logs;
- outbound HTTPS access to GitHub and Claude services;
- a process supervisor such as `systemd` or `launchd`.

Remote worker host, only when `remote.enabled: true`:

- SSH reachable from the coordinator;
- `symphony-worker` on `PATH`;
- `git` and authenticated `claude` CLI;
- writable `remote.workspace_root`, `remote.artifact_root`, and
  `remote.session_store`;
- no tracker API token in the remote environment unless explicitly accepted by
  the operator.

## Filesystem Layout

Use stable absolute paths for production. Example:

```text
/opt/symphony/symphony-cc
/etc/symphony/WORKFLOW.md
/etc/symphony/symphony.env
/var/lib/symphony/workspaces
/var/lib/symphony/sessions
/var/lib/symphony/transcripts
/var/lib/symphony/runs
/var/log/symphony/symphony.log
```

Recommended permissions:

```bash
sudo install -d -m 0750 -o symphony -g symphony /etc/symphony /var/lib/symphony /var/log/symphony
sudo install -d -m 0750 -o symphony -g symphony /var/lib/symphony/workspaces /var/lib/symphony/sessions /var/lib/symphony/transcripts /var/lib/symphony/runs
sudo install -m 0640 -o symphony -g symphony symphony.env /etc/symphony/symphony.env
sudo install -m 0640 -o symphony -g symphony WORKFLOW.md /etc/symphony/WORKFLOW.md
```

## Workflow Baseline

Start from `WORKFLOW.example.md`, then change paths to absolute production
paths:

```yaml
tracker:
  kind: github
  owner: <owner>
  repo: <repo>
  token: $GITHUB_TOKEN
  include_labels: ["symphony-ready"]
  exclude_labels: ["symphony-running", "symphony-blocked"]

agent:
  provider: claude_code
  max_concurrency: 1
  max_turns: 3

workspace:
  root: /var/lib/symphony/workspaces
  populate: git

github:
  claim_label: symphony-running
  ready_label: symphony-ready
  blocked_label: symphony-blocked
  done_label: symphony-done
  branch_prefix: symphony
  base_branch: main
  draft_pr: true
  claim_comment: true
  pr_link_comment: true

security:
  profile: trusted_unattended

claude:
  model: claude-opus-4-7
  permission_mode: bypassPermissions
  session_store: /var/lib/symphony/sessions
  transcript_store: /var/lib/symphony/transcripts
  artifact_store: /var/lib/symphony/runs
  turn_timeout_ms: 3600000
  stall_timeout_ms: 300000
  retry_resume_policy: resume_same_session
```

Use `trusted_unattended` plus `bypassPermissions` only on trusted hosts,
trusted repositories, and trusted issue sources. For a safer human-supervised
pilot, use `security.profile: conservative` and `permission_mode: acceptEdits`,
but expect tasks that need shell or `gh` to stop with permission evidence rather
than completing a PR.

## Environment File

Example `/etc/symphony/symphony.env`:

```bash
GITHUB_TOKEN=github_pat_or_ghp_value
PYTHONUNBUFFERED=1
```

For remote workers:

```bash
SYMPHONY_REMOTE_GIT_TOKEN=git_only_token_value
```

Avoid putting Anthropic or GitHub credentials in command-line arguments because
they may appear in process listings or supervisor logs.

## Preflight Checklist

Run these as the service user:

```bash
cd /opt/symphony/symphony-cc
python -m pip install -e ".[dev]"
make ci
claude --version
python -c "import claude_agent_sdk; print('claude-agent-sdk ok')"
GITHUB_TOKEN="$GITHUB_TOKEN" gh auth status
python -c "from symphony.workflow import load_workflow; print(load_workflow('/etc/symphony/WORKFLOW.md').config.tracker.repo)"
```

Then run the live targets required by the deployment topology:

```bash
make failure-drills
make security-audit
make live-github
make live-claude
make live-e2e
```

For the full PR-producing E2E proof on a trusted disposable issue, run:

```bash
SYMPHONY_E2E_PERMISSION_MODE=bypassPermissions SYMPHONY_E2E_REQUIRE_PR=1 make live-e2e
```

Add these before enabling the matching features:

```bash
make live-remote-claude       # required before remote.enabled: true
make live-concurrency-e2e     # required before agent.max_concurrency > 1
```

Record the evidence listed in `docs/production-readiness.md`.

## systemd Service

Example `/etc/systemd/system/symphony.service`:

```ini
[Unit]
Description=Symphony GitHub Claude Code orchestrator
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=symphony
Group=symphony
WorkingDirectory=/opt/symphony/symphony-cc
EnvironmentFile=/etc/symphony/symphony.env
ExecStart=/opt/symphony/symphony-cc/.venv/bin/symphony run --workflow /etc/symphony/WORKFLOW.md --dashboard --dashboard-port 8080 --log-level info
Restart=on-failure
RestartSec=15
TimeoutStopSec=60
KillSignal=SIGTERM
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

Install and operate:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now symphony
sudo systemctl status symphony
journalctl -u symphony -f
```

Dashboard:

```bash
curl -fsS http://127.0.0.1:8080/health
curl -fsS http://127.0.0.1:8080/status.json
```

Keep the dashboard bound to localhost unless an external auth/proxy layer is
provided by the operator.

## launchd Service

For macOS pilots, create
`~/Library/LaunchAgents/com.symphony.orchestrator.plist` for the service user:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.symphony.orchestrator</string>
  <key>WorkingDirectory</key>
  <string>/opt/symphony/symphony-cc</string>
  <key>ProgramArguments</key>
  <array>
    <string>/opt/symphony/symphony-cc/.venv/bin/symphony</string>
    <string>run</string>
    <string>--workflow</string>
    <string>/etc/symphony/WORKFLOW.md</string>
    <string>--dashboard</string>
    <string>--dashboard-port</string>
    <string>8080</string>
    <string>--log-level</string>
    <string>info</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>GITHUB_TOKEN</key>
    <string>replace-with-launchd-secret-management</string>
    <key>PYTHONUNBUFFERED</key>
    <string>1</string>
  </dict>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>/var/log/symphony/symphony.log</string>
  <key>StandardErrorPath</key>
  <string>/var/log/symphony/symphony.err.log</string>
</dict>
</plist>
```

Prefer an operator-managed secret injection mechanism over storing real tokens
directly in the plist.

Operate:

```bash
launchctl load ~/Library/LaunchAgents/com.symphony.orchestrator.plist
launchctl kickstart gui/$(id -u)/com.symphony.orchestrator
launchctl print gui/$(id -u)/com.symphony.orchestrator
launchctl unload ~/Library/LaunchAgents/com.symphony.orchestrator.plist
```

## Start, Stop, Restart

Start:

```bash
sudo systemctl start symphony
```

Stop:

```bash
sudo systemctl stop symphony
```

Restart after workflow or package changes:

```bash
sudo systemctl restart symphony
```

Do not delete workspaces or artifacts during a restart. Restart recovery uses
GitHub labels and local filesystem evidence to reconcile active work.

## Recovery Procedures

### Daemon Crash Or Host Reboot

1. Start the service.
2. Check `/status.json` and recent logs.
3. Inspect issues with `symphony-running`.
4. Confirm each issue has a recent PR, terminal artifact, retry entry, or block
   comment.
5. If an issue is stuck with no active worker and no PR, remove
   `symphony-running` only after saving the relevant artifacts.

### Invalid Workflow Reload

1. Check logs for reload rejection.
2. Fix `/etc/symphony/WORKFLOW.md`.
3. Wait for the next poll cycle or restart the service.
4. Confirm the workflow revision changes in `/status.json`.

### GitHub Rate Limit Or 5xx

1. Leave the daemon running unless it is hot-looping.
2. Check retry queue and blocker comments.
3. Reduce `agent.max_concurrency` if needed.
4. File a follow-up issue if PR evidence lookup becomes inconclusive.

### Claude 503 Or Provider Failure

1. Inspect `terminal.json` and provider event artifacts.
2. Confirm the outcome is not `completed_with_pr` unless GitHub evidence exists.
3. Let retry policy resume the same session when configured.
4. If failures repeat, block the issue with the artifact path and stop the
   service before changing provider settings.

### Remote Worker Failure

1. Check coordinator artifacts first.
2. SSH to the remote host and inspect remote artifact root.
3. Verify no tracker token appears in remote worker payloads or logs.
4. Check SSH reachability, remote disk, remote `symphony-worker`, and remote
   `claude` auth.
5. Re-run `make live-remote-claude` before returning remote workers to service.

## Artifact And Cleanup Policy

Artifacts are audit evidence. Keep them by default during pilots.

Recommended pilot policy:

- retain all artifacts for at least 30 days;
- enable workspace cleanup only after at least one successful L4 operated pilot;
- archive evidence JSON, `terminal.json`, and PR URLs before deleting a
  workspace;
- never run manual cleanup while workers are active.

## Production Go/No-Go

Go:

- `make ci` passes;
- required live targets pass for the chosen topology;
- one full issue to PR E2E has fresh evidence;
- dashboard health and `/status.json` are reachable locally;
- supervisor restart has been tested;
- stop/restart/recovery procedures have been rehearsed;
- secrets are loaded from environment files or a secret manager, not workflow
  literals.

No-go:

- default CI fails;
- live E2E was skipped accidentally;
- remote workers are enabled without remote Claude E2E evidence;
- concurrency is raised without concurrency E2E evidence;
- a provider error is reported as completed work;
- token-like values appear in logs, dashboard, status JSON, or artifacts.
