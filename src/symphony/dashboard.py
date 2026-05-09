"""Static operator dashboard renderer (#56).

The dashboard is intentionally a pure renderer over the read-only status
snapshot. It does not start a server, mutate daemon state, or require a
database.
"""

from __future__ import annotations

import json
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import quote


def render_dashboard_html(snapshot: dict[str, Any]) -> str:
    """Render a human-readable HTML dashboard from a status snapshot."""

    title = f"Symphony Dashboard - {_text(snapshot.get('state', 'unknown'))}"
    body = "\n".join(
        [
            _summary(snapshot),
            _active_workers(snapshot.get("active_workers", [])),
            _retry_queue(snapshot.get("retry_queue", [])),
            _recent_finished(snapshot.get("recent_finished", [])),
            _recovery_decisions(snapshot.get("recovery_decisions", [])),
        ]
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(title)}</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f7f8fa;
      --panel: #ffffff;
      --text: #1f2933;
      --muted: #667085;
      --line: #d6dae1;
      --ok: #137333;
      --warn: #b45309;
      --bad: #b42318;
      --info: #175cd3;
    }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font: 14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}
    header, main {{
      max-width: 1180px;
      margin: 0 auto;
      padding: 20px 24px;
    }}
    header {{
      display: flex;
      align-items: baseline;
      justify-content: space-between;
      border-bottom: 1px solid var(--line);
    }}
    h1 {{
      margin: 0;
      font-size: 22px;
      font-weight: 650;
    }}
    h2 {{
      margin: 0 0 12px;
      font-size: 16px;
    }}
    section {{
      margin: 18px 0;
      padding: 16px;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
    }}
    th, td {{
      padding: 8px 10px;
      border-top: 1px solid var(--line);
      text-align: left;
      vertical-align: top;
    }}
    th {{
      color: var(--muted);
      font-weight: 600;
      white-space: nowrap;
    }}
    code {{
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 12px;
    }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
      gap: 10px;
    }}
    .metric {{
      padding: 10px 12px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #fbfcfd;
    }}
    .label {{
      color: var(--muted);
      font-size: 12px;
    }}
    .value {{
      margin-top: 4px;
      font-weight: 650;
    }}
    .badge {{
      display: inline-block;
      padding: 2px 8px;
      border-radius: 999px;
      font-size: 12px;
      font-weight: 650;
      background: #e9eef8;
      color: var(--info);
    }}
    .ok {{ color: var(--ok); }}
    .warn {{ color: var(--warn); }}
    .bad {{ color: var(--bad); }}
    .muted {{ color: var(--muted); }}
  </style>
</head>
<body>
  <header>
    <h1>Symphony Runtime</h1>
    <span class="muted">{_text(snapshot.get("timestamp"))}</span>
  </header>
  <main>
    {body}
  </main>
</body>
</html>
"""


def render_run_detail_html(snapshot: dict[str, Any], issue_identifier: str) -> str | None:
    """Render a detail page for one issue/run, or ``None`` when absent."""

    detail = run_detail(snapshot, issue_identifier)
    if detail is None:
        return None

    title = f"Symphony Run - {_text(issue_identifier)}"
    body = "\n".join(
        [
            _detail_summary(detail),
            _detail_events(detail),
            _detail_json("Active Worker", detail.get("active_worker")),
            _detail_json("Retry State", detail.get("retry_state")),
            _detail_json("Finished Run", detail.get("finished_run")),
            _detail_json("Recovery Decisions", detail.get("recovery_decisions")),
        ]
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(title)}</title>
  <style>
    body {{
      margin: 0;
      background: #f7f8fa;
      color: #1f2933;
      font: 14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}
    header, main {{
      max-width: 980px;
      margin: 0 auto;
      padding: 20px 24px;
    }}
    header {{
      display: flex;
      align-items: baseline;
      justify-content: space-between;
      border-bottom: 1px solid #d6dae1;
    }}
    h1 {{ margin: 0; font-size: 22px; }}
    h2 {{ margin: 0 0 12px; font-size: 16px; }}
    section {{
      margin: 18px 0;
      padding: 16px;
      background: #fff;
      border: 1px solid #d6dae1;
      border-radius: 8px;
    }}
    table {{ width: 100%; border-collapse: collapse; }}
    th, td {{
      padding: 8px 10px;
      border-top: 1px solid #d6dae1;
      text-align: left;
      vertical-align: top;
    }}
    th {{ color: #667085; white-space: nowrap; }}
    code, pre {{
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 12px;
    }}
    pre {{
      overflow: auto;
      padding: 12px;
      background: #fbfcfd;
      border: 1px solid #d6dae1;
      border-radius: 6px;
    }}
    .muted {{ color: #667085; }}
    .bad {{ color: #b42318; }}
    .warn {{ color: #b45309; }}
    .ok {{ color: #137333; }}
  </style>
</head>
<body>
  <header>
    <h1>{escape(_text(issue_identifier))}</h1>
    <a href="/">Back to dashboard</a>
  </header>
  <main>
    {body}
  </main>
</body>
</html>
"""


def run_detail(snapshot: dict[str, Any], issue_identifier: str) -> dict[str, Any] | None:
    """Return read-only detail data for one issue across status surfaces."""

    active = _find_by_issue(snapshot.get("active_workers", []), issue_identifier)
    retry = _find_by_issue(snapshot.get("retry_queue", []), issue_identifier)
    finished = _find_by_issue(snapshot.get("recent_finished", []), issue_identifier)
    recovery = [
        item
        for item in snapshot.get("recovery_decisions", [])
        if item.get("issue_identifier") == issue_identifier
    ]

    if active is None and retry is None and finished is None and not recovery:
        return None

    return {
        "issue_identifier": issue_identifier,
        "active_worker": active,
        "retry_state": retry,
        "finished_run": finished,
        "recovery_decisions": recovery,
    }


def write_dashboard_html(snapshot: dict[str, Any], path: Path | str) -> Path:
    """Write the rendered dashboard to ``path`` and return the path."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_dashboard_html(snapshot), encoding="utf-8")
    return target


def _summary(snapshot: dict[str, Any]) -> str:
    capacity = snapshot.get("capacity", {})
    workflow = snapshot.get("workflow", {})
    security = snapshot.get("security", {})
    state = _text(snapshot.get("state", "unknown"))
    return f"""
<section>
  <h2>Summary</h2>
  <div class="grid">
    {_metric("State", f'<span class="badge">{escape(state)}</span>')}
    {_metric("Run", _code(snapshot.get("run_id")))}
    {_metric("Active", _text(capacity.get("active", 0)))}
    {_metric("Max Concurrency", _text(capacity.get("max_concurrency", 0)))}
    {_metric("Workflow Revision", _text(workflow.get("revision")))}
    {_metric("Workflow Path", _code(workflow.get("path")))}
    {_metric("Security Profile", _text(security.get("profile")))}
    {_metric("Permission Mode", _text(security.get("permission_mode")))}
  </div>
</section>
"""


def _active_workers(workers: list[dict[str, Any]]) -> str:
    rows = []
    for worker in workers:
        last = worker.get("last_event") or {}
        payload = last.get("payload") or {}
        permission_denials = payload.get("permission_denials") or []
        warning = ""
        if permission_denials:
            warning = f'<div class="warn">permission denials: {len(permission_denials)}</div>'
        rows.append(
            "<tr>"
            f"<td>{_issue_link(worker)}{_detail_link(worker.get('issue_identifier'))}</td>"
            f"<td>{_text(worker.get('lane'))}</td>"
            f"<td>{_code(worker.get('provider_session_id'))}</td>"
            f"<td>{_text(worker.get('attempt'))}</td>"
            f"<td>{_text(worker.get('security_profile'))}</td>"
            f"<td>{_code(worker.get('artifact_dir'))}</td>"
            f"<td>{_usage_cell(worker.get('usage'))}</td>"
            f"<td>{_event_cell(last)}{warning}</td>"
            f"<td>{_session_signal_cell(worker)}</td>"
            "</tr>"
        )
    return _table(
        "Active Workers",
        (
            "Issue",
            "Lane",
            "Provider Session",
            "Attempt",
            "Security Profile",
            "Artifacts",
            "Usage",
            "Last Event",
            "Session Signal",
        ),
        rows,
    )


def _retry_queue(retries: list[dict[str, Any]]) -> str:
    rows = [
        "<tr>"
        f"<td>{_code(retry.get('issue_identifier'))}{_detail_link(retry.get('issue_identifier'))}</td>"
        f"<td>{_text(retry.get('attempts'))}</td>"
        f"<td>{_text(retry.get('next_attempt_at'))}</td>"
        f"<td class=\"warn\">{_text(retry.get('last_error'))}</td>"
        "</tr>"
        for retry in retries
    ]
    return _table(
        "Retry Queue",
        ("Issue", "Attempts", "Next Attempt", "Last Error"),
        rows,
    )


def _recent_finished(items: list[dict[str, Any]]) -> str:
    rows = []
    for item in items:
        css = _outcome_class(item)
        rows.append(
            "<tr>"
            f"<td>{_text(item.get('issue_identifier'))}{_detail_link(item.get('issue_identifier'))}</td>"
            f"<td>{_text(item.get('lane'))}</td>"
            f"<td class=\"{css}\">{_text(item.get('terminal_state'))}</td>"
            f"<td class=\"{css}\">{_text(item.get('task_outcome'))}</td>"
            f"<td>{_code(item.get('provider_session_id'))}</td>"
            f"<td>{_text(item.get('security_profile'))}</td>"
            f"<td>{_code(item.get('artifact_dir'))}</td>"
            f"<td>{_usage_cell(item.get('usage'))}</td>"
            f"<td>{_finished_signals_cell(item)}</td>"
            f"<td>{_text(item.get('last_event_at'))}</td>"
            "</tr>"
        )
    return _table(
        "Recent Finished",
        (
            "Issue",
            "Lane",
            "Terminal",
            "Task Outcome",
            "Provider Session",
            "Security Profile",
            "Artifacts",
            "Usage",
            "Signals",
            "Last Event",
        ),
        rows,
    )


def _recovery_decisions(items: list[dict[str, Any]]) -> str:
    rows = [
        "<tr>"
        f"<td>{_text(item.get('issue_identifier'))}{_detail_link(item.get('issue_identifier'))}</td>"
        f"<td>{_text(item.get('action'))}</td>"
        f"<td>{_text(item.get('reason'))}</td>"
        "</tr>"
        for item in items
    ]
    return _table("Recovery Decisions", ("Issue", "Action", "Reason"), rows)


def _table(title: str, headers: tuple[str, ...], rows: list[str]) -> str:
    if not rows:
        rows = [f"<tr><td colspan=\"{len(headers)}\" class=\"muted\">None</td></tr>"]
    head = "".join(f"<th>{escape(h)}</th>" for h in headers)
    return f"""
<section>
  <h2>{escape(title)}</h2>
  <table>
    <thead><tr>{head}</tr></thead>
    <tbody>
      {"".join(rows)}
    </tbody>
  </table>
</section>
"""


def _metric(label: str, value_html: str) -> str:
    return (
        '<div class="metric">'
        f'<div class="label">{escape(label)}</div>'
        f'<div class="value">{value_html}</div>'
        "</div>"
    )


def _issue_link(worker: dict[str, Any]) -> str:
    ident = _text(worker.get("issue_identifier"))
    url = worker.get("issue_url")
    if isinstance(url, str) and url:
        return f'<a href="{escape(url, quote=True)}">{escape(ident)}</a>'
    return escape(ident)


def _detail_link(issue_identifier: Any) -> str:
    ident = _text(issue_identifier)
    if not ident:
        return ""
    href = f"/runs/{quote(ident, safe='')}"
    return f'<div><a class="muted" href="{escape(href, quote=True)}">details</a></div>'


def _event_cell(event: dict[str, Any]) -> str:
    if not event:
        return '<span class="muted">None</span>'
    return (
        f"<div>{escape(_text(event.get('event')))}</div>"
        f"<div class=\"muted\">{escape(_text(event.get('timestamp')))}</div>"
    )


def _session_signal_cell(worker: dict[str, Any]) -> str:
    events = worker.get("recent_events")
    if not isinstance(events, list) or not events:
        return '<span class="muted">None</span>'
    snippets: list[str] = []
    for event in events[-4:]:
        if not isinstance(event, dict):
            continue
        rendered = _session_event_summary(event)
        if rendered:
            snippets.append(f"<div>{escape(rendered)}</div>")
    return "".join(snippets) if snippets else '<span class="muted">None</span>'


def _session_event_summary(event: dict[str, Any]) -> str:
    name = _text(event.get("event"))
    payload = event.get("payload") or {}
    if isinstance(payload, dict):
        if name == "message_delta":
            text = payload.get("text")
            if text:
                return f"message: {_truncate(_text(text), 140)}"
        tool_name = payload.get("tool_name")
        if name == "tool_started" and tool_name:
            return f"tool_started: {tool_name}"
        if name == "tool_completed":
            result = payload.get("content") or payload.get("tool_use_result")
            return f"tool_completed: {_truncate(_text(result), 140)}"
        if name == "heartbeat":
            data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
            description = data.get("description") if isinstance(data, dict) else None
            task_id = data.get("task_id") if isinstance(data, dict) else None
            if description or task_id:
                return f"heartbeat: {_text(description)} {_text(task_id)}".strip()
    return name


def _truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "…"


def _usage_cell(usage: Any) -> str:
    if not isinstance(usage, dict):
        return '<span class="muted">None</span>'
    total = _text(usage.get("total_tokens"))
    cost = _text(usage.get("cost_usd"))
    if cost:
        return f"<div>{escape(total)} tokens</div><div class=\"muted\">${escape(cost)}</div>"
    return f"<div>{escape(total)} tokens</div>"


def _finished_signals_cell(item: dict[str, Any]) -> str:
    parts: list[str] = []
    denials = item.get("permission_denials_count")
    if denials:
        parts.append(f'<div class="bad">permission denials: {escape(_text(denials))}</div>')
    no_pr_reason = item.get("no_pr_reason")
    if no_pr_reason:
        parts.append(f'<div>no PR: {escape(_text(no_pr_reason))}</div>')
    evidence = item.get("task_evidence")
    if isinstance(evidence, list):
        for entry in evidence[:4]:
            if isinstance(entry, dict):
                rendered = _task_evidence_text(entry)
                if rendered:
                    parts.append(f"<div>{escape(rendered)}</div>")
    decided_by = item.get("outcome_decided_by")
    if decided_by:
        parts.append(f'<div class="muted">decided by: {escape(_text(decided_by))}</div>')
    if not parts:
        return '<span class="muted">None</span>'
    return "".join(parts)


def _task_evidence_text(entry: dict[str, Any]) -> str:
    kind = entry.get("type")
    if kind == "permission_denied":
        tool_names = entry.get("tool_names") or []
        tools = ", ".join(str(name) for name in tool_names) if tool_names else "unknown"
        return (
            "permission_denied: "
            f"denials_count={entry.get('denials_count', 0)}, tool_names={tools}"
        )
    if kind == "no_pr_declared":
        return f"no_pr_declared: {entry.get('reason', '')}"
    if kind == "pr_linked":
        return f"pr_linked: #{entry.get('number', '')} {entry.get('url', '')}"
    if kind == "branch_pushed":
        return f"branch_pushed: {entry.get('name', '')}"
    if kind == "diff_in_workspace":
        return (
            "diff_in_workspace: "
            f"files_changed={entry.get('files_changed', 0)}, "
            f"lines_added={entry.get('lines_added', 0)}, "
            f"lines_removed={entry.get('lines_removed', 0)}"
        )
    if kind:
        return _text(entry)
    return ""


def _outcome_class(item: dict[str, Any]) -> str:
    outcome = _text(item.get("task_outcome"))
    terminal = _text(item.get("terminal_state"))
    if "incomplete" in outcome or "blocked" in outcome or terminal == "failed":
        return "bad"
    if "retry" in outcome or terminal == "cancelled":
        return "warn"
    return "ok"


def _code(value: Any) -> str:
    return f"<code>{escape(_text(value))}</code>"


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, int | float | bool):
        return str(value)
    return json.dumps(value, sort_keys=True)


def _find_by_issue(items: Any, issue_identifier: str) -> dict[str, Any] | None:
    if not isinstance(items, list):
        return None
    for item in items:
        if isinstance(item, dict) and item.get("issue_identifier") == issue_identifier:
            return item
    return None


def _detail_summary(detail: dict[str, Any]) -> str:
    issue = _text(detail.get("issue_identifier"))
    active = detail.get("active_worker") or {}
    finished = detail.get("finished_run") or {}
    retry = detail.get("retry_state") or {}
    row = {
        "state": _detail_state(detail),
        "lane": active.get("lane") or finished.get("lane"),
        "terminal_state": finished.get("terminal_state") or active.get("terminal_state"),
        "task_outcome": finished.get("task_outcome"),
        "provider_session_id": active.get("provider_session_id")
        or finished.get("provider_session_id"),
        "artifact_dir": active.get("artifact_dir") or finished.get("artifact_dir"),
        "last_error": retry.get("last_error") or active.get("error"),
    }
    rows = [
        "<tr>"
        f"<th>{escape(key.replace('_', ' ').title())}</th>"
        f"<td>{_detail_value(key, value)}</td>"
        "</tr>"
        for key, value in row.items()
        if value is not None
    ]
    return f"""
<section>
  <h2>Run Summary</h2>
  <table>
    <tbody>
      <tr><th>Issue</th><td>{escape(issue)}</td></tr>
      {"".join(rows)}
    </tbody>
  </table>
</section>
"""


def _detail_state(detail: dict[str, Any]) -> str:
    if detail.get("active_worker"):
        return "active"
    if detail.get("retry_state"):
        return "retry_waiting"
    if detail.get("finished_run"):
        return "finished"
    return "recovered"


def _detail_value(key: str, value: Any) -> str:
    if key in {"provider_session_id", "artifact_dir"}:
        return _code(value)
    return escape(_text(value))


def _detail_events(detail: dict[str, Any]) -> str:
    active = detail.get("active_worker") or {}
    finished = detail.get("finished_run") or {}
    retry = detail.get("retry_state") or {}
    event = active.get("last_event") or {}
    rows = [
        ("Last Event", event.get("event")),
        ("Last Event At", event.get("timestamp") or finished.get("last_event_at")),
        ("Retry Attempts", retry.get("attempts")),
        ("Next Attempt", retry.get("next_attempt_at")),
    ]
    html_rows = [
        "<tr>"
        f"<th>{escape(label)}</th>"
        f"<td>{escape(_text(value))}</td>"
        "</tr>"
        for label, value in rows
        if value is not None
    ]
    payload = event.get("payload")
    payload_html = ""
    if payload:
        payload_html = (
            "<h2>Last Event Payload</h2>"
            f"<pre>{escape(json.dumps(payload, indent=2, sort_keys=True))}</pre>"
        )
    recent_events = active.get("recent_events")
    recent_html = ""
    if isinstance(recent_events, list) and recent_events:
        rows = []
        for item in recent_events[-20:]:
            if not isinstance(item, dict):
                continue
            rows.append(
                "<tr>"
                f"<td>{escape(_text(item.get('timestamp')))}</td>"
                f"<td>{escape(_text(item.get('event')))}</td>"
                f"<td>{escape(_session_event_summary(item))}</td>"
                "</tr>"
            )
        recent_html = _table(
            "Recent Session Events",
            ("Timestamp", "Event", "Summary"),
            rows,
        )
    return f"""
<section>
  <h2>Runtime Signals</h2>
  <table><tbody>{"".join(html_rows)}</tbody></table>
  {payload_html}
</section>
{recent_html}
"""


def _detail_json(title: str, value: Any) -> str:
    if not value:
        return f"""
<section>
  <h2>{escape(title)}</h2>
  <p class="muted">None</p>
</section>
"""
    return f"""
<section>
  <h2>{escape(title)}</h2>
  <pre>{escape(json.dumps(value, indent=2, sort_keys=True))}</pre>
</section>
"""


__all__ = [
    "render_dashboard_html",
    "render_run_detail_html",
    "run_detail",
    "write_dashboard_html",
]
