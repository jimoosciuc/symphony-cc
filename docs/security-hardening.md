# Security Hardening

This document is the production security checklist for Symphony. It focuses on
GitHub-first and Claude Code-first deployments.

Run the offline audit target before any trusted pilot:

```bash
make security-audit
```

The audit target groups tests for security profiles, secret redaction, dashboard
and artifact output, GitHub GraphQL tool envelopes, and remote-worker credential
boundaries.

## Credential Boundaries

| Credential | Where it may exist | Where it must not exist |
| --- | --- | --- |
| `GITHUB_TOKEN` tracker token | Coordinator environment, coordinator config object, GitHub API requests. | Remote worker snapshot, SSH command arguments, dashboard HTML, `/status.json`, artifacts, provider-visible tool output. |
| `SYMPHONY_REMOTE_GIT_TOKEN` | Remote worker snapshot only when it is distinct from the tracker token and needed for `workspace.populate: git`. | GitHub tracker operations, issue comments, dashboard, status JSON, logs after redaction. |
| Claude auth | OS user profile or approved secret mechanism for the host running Claude. | `WORKFLOW.md`, GitHub issues, PR comments, artifacts. |
| GitHub Actions live secrets | Manual live workflow environment only. | Default PR CI, committed docs, committed workflow examples. |

Remote workers are trusted code-execution hosts. Treat a remote host that
receives `SYMPHONY_REMOTE_GIT_TOKEN` as trusted for repository contents, but not
automatically trusted for tracker API access. The coordinator tracker token must
not be serialized to the worker.

## GitHub Token Scopes

Recommended fine-grained PAT permissions for one target repository:

- Metadata: read;
- Issues: read/write;
- Contents: read/write;
- Pull requests: read/write.

Optional tools or custom workflows may need additional scopes. Add those only
for the repository and workflow that requires them. Prefer one token per
deployment target so revocation is narrow.

## Security Profiles

`security.profile` communicates operator intent and validates dangerous
combinations:

| Profile | Recommended use | Permission guidance |
| --- | --- | --- |
| `restricted` | Read-heavy, human-supervised, or low-trust issue sources. | Rejects `claude.permission_mode: bypassPermissions`. |
| `conservative` | Default local development and early pilots. | Allows normal modes, warns on high-trust choices. |
| `trusted_unattended` | Trusted host, trusted repo, trusted issue labels, unattended PR creation. | May use `bypassPermissions`; warnings are expected and must be accepted by the operator. |

Do not use `trusted_unattended` for public issue intake without a separate
triage gate.

## Redaction Requirements

The following surfaces must redact token-like values and configured secret keys:

- provider events and `events.jsonl`;
- `request.json`, `session.json`, `terminal.json`, and retention reports;
- dashboard HTML;
- `/status.json` and `/runs/<issue>.json`;
- remote worker event streams;
- SSH/SCP stderr and transport errors;
- GitHub GraphQL tool results returned to Claude.

Redaction is defense in depth, not permission control. The primary control is
keeping secrets out of prompts, issue bodies, PR comments, command-line
arguments, and remote worker payloads.

## Remote Worker Rules

- Remote snapshot `tracker.token` must be the placeholder
  `remote-worker-no-tracker-token`.
- Remote `git_token` may be included only when it is configured and distinct
  from the coordinator tracker token.
- SSH commands must use staged payload paths, not inline serialized secrets.
- Coordinator-side artifact collection must redact again after copying remote
  artifacts.
- Any remote token exposure is a production no-go until fixed.

## Operator Checklist

Before a production pilot:

1. Run `make ci`.
2. Run `make security-audit`.
3. Confirm `WORKFLOW.md` uses `$GITHUB_TOKEN`, not a literal token.
4. Confirm remote workers are disabled unless `make live-remote-claude` passed.
5. Confirm the service user's shell can run `claude --version` without printing
   credentials.
6. Confirm dashboard access is localhost-only or protected by an operator-owned
   auth layer.
7. Inspect a fresh `terminal.json`, `/status.json`, and dashboard page for
   token-like values.

## No-Go Conditions

Do not proceed when any are true:

- a tracker token appears in a remote worker snapshot or SSH command;
- a token-like value appears in dashboard, JSON, artifacts, or logs;
- `restricted` is combined with `bypassPermissions`;
- remote workers are enabled without a separate remote git credential;
- public or untrusted issue intake can trigger `trusted_unattended` execution;
- default PR CI requires live secrets.
