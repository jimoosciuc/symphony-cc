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
            f"<td>{_issue_link(worker)}</td>"
            f"<td>{_text(worker.get('lane'))}</td>"
            f"<td>{_code(worker.get('provider_session_id'))}</td>"
            f"<td>{_text(worker.get('attempt'))}</td>"
            f"<td>{_text(worker.get('security_profile'))}</td>"
            f"<td>{_code(worker.get('artifact_dir'))}</td>"
            f"<td>{_usage_cell(worker.get('usage'))}</td>"
            f"<td>{_event_cell(last)}{warning}</td>"
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
        ),
        rows,
    )


def _retry_queue(retries: list[dict[str, Any]]) -> str:
    rows = [
        "<tr>"
        f"<td>{_code(retry.get('issue_identifier'))}</td>"
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
            f"<td>{_text(item.get('issue_identifier'))}</td>"
            f"<td>{_text(item.get('lane'))}</td>"
            f"<td class=\"{css}\">{_text(item.get('terminal_state'))}</td>"
            f"<td class=\"{css}\">{_text(item.get('task_outcome'))}</td>"
            f"<td>{_code(item.get('provider_session_id'))}</td>"
            f"<td>{_text(item.get('security_profile'))}</td>"
            f"<td>{_code(item.get('artifact_dir'))}</td>"
            f"<td>{_usage_cell(item.get('usage'))}</td>"
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
            "Last Event",
        ),
        rows,
    )


def _recovery_decisions(items: list[dict[str, Any]]) -> str:
    rows = [
        "<tr>"
        f"<td>{_text(item.get('issue_identifier'))}</td>"
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


def _event_cell(event: dict[str, Any]) -> str:
    if not event:
        return '<span class="muted">None</span>'
    return (
        f"<div>{escape(_text(event.get('event')))}</div>"
        f"<div class=\"muted\">{escape(_text(event.get('timestamp')))}</div>"
    )


def _usage_cell(usage: Any) -> str:
    if not isinstance(usage, dict):
        return '<span class="muted">None</span>'
    total = _text(usage.get("total_tokens"))
    cost = _text(usage.get("cost_usd"))
    if cost:
        return f"<div>{escape(total)} tokens</div><div class=\"muted\">${escape(cost)}</div>"
    return f"<div>{escape(total)} tokens</div>"


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


__all__ = ["render_dashboard_html", "write_dashboard_html"]
