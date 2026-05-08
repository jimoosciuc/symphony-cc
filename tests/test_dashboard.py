"""Tests for the static operator dashboard renderer (#56)."""

from __future__ import annotations

from pathlib import Path

from symphony.dashboard import render_dashboard_html, write_dashboard_html


def _snapshot() -> dict:
    return {
        "timestamp": "2026-05-08T00:00:00+00:00",
        "run_id": "run-123",
        "state": "running",
        "workflow": {
            "revision": 2,
            "path": "/tmp/WORKFLOW.md",
            "loaded_at": "2026-05-08T00:00:00+00:00",
        },
        "capacity": {"active": 1, "max_concurrency": 2},
        "active_workers": [
            {
                "issue_identifier": "acme/proj#1",
                "issue_url": "https://github.com/acme/proj/issues/1",
                "artifact_dir": "/tmp/artifacts/acme_proj_1/1",
                "provider_session_id": "provider-1",
                "attempt": 1,
                "last_event": {
                    "event": "message_delta",
                    "timestamp": "2026-05-08T00:00:01+00:00",
                    "payload": {"permission_denials": [{"tool": "Bash"}]},
                },
            }
        ],
        "retry_queue": [
            {
                "issue_identifier": "acme/proj#2",
                "attempts": 2,
                "next_attempt_at": "2026-05-08T00:01:00+00:00",
                "last_error": "temporary failure",
            }
        ],
        "recent_finished": [
            {
                "issue_identifier": "acme/proj#3",
                "terminal_state": "completed",
                "task_outcome": "completed_with_pr",
                "provider_session_id": "provider-3",
                "artifact_dir": "/tmp/artifacts/acme_proj_3/1",
                "last_event_at": "2026-05-08T00:02:00+00:00",
            },
            {
                "issue_identifier": "acme/proj#4",
                "terminal_state": "failed",
                "task_outcome": "blocked_operator_required",
                "provider_session_id": "provider-4",
                "artifact_dir": "/tmp/artifacts/acme_proj_4/1",
                "last_event_at": "2026-05-08T00:03:00+00:00",
            },
        ],
        "recovery_decisions": [
            {
                "issue_identifier": "acme/proj#5",
                "action": "released",
                "reason": "issue closed",
            }
        ],
    }


def test_dashboard_renders_core_operator_states() -> None:
    html = render_dashboard_html(_snapshot())

    assert "Symphony Runtime" in html
    assert "running" in html
    assert "acme/proj#1" in html
    assert "provider-1" in html
    assert "permission denials: 1" in html
    assert "temporary failure" in html
    assert "completed_with_pr" in html
    assert "blocked_operator_required" in html
    assert "issue closed" in html


def test_dashboard_escapes_snapshot_values() -> None:
    snapshot = _snapshot()
    snapshot["active_workers"][0]["issue_identifier"] = "<script>alert(1)</script>"
    snapshot["active_workers"][0]["issue_url"] = 'https://example.com/"bad"'

    html = render_dashboard_html(snapshot)

    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert 'href="https://example.com/&quot;bad&quot;"' in html


def test_write_dashboard_html_creates_parent_directory(tmp_path: Path) -> None:
    target = tmp_path / "out" / "dashboard.html"

    written = write_dashboard_html(_snapshot(), target)

    assert written == target
    assert target.exists()
    assert "Symphony Runtime" in target.read_text(encoding="utf-8")
