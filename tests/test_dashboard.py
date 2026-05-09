"""Tests for the static operator dashboard renderer (#56)."""

from __future__ import annotations

from pathlib import Path

from symphony.dashboard import (
    render_dashboard_html,
    render_run_detail_html,
    run_detail,
    write_dashboard_html,
)


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
        "security": {"profile": "trusted_unattended", "permission_mode": "acceptEdits"},
        "capacity": {"active": 1, "max_concurrency": 2},
        "active_workers": [
            {
                "issue_identifier": "acme/proj#1",
                "issue_url": "https://github.com/acme/proj/issues/1",
                "artifact_dir": "/tmp/artifacts/acme_proj_1/1",
                "provider_session_id": "provider-1",
                "lane": "implementer",
                "attempt": 1,
                "security_profile": "trusted_unattended",
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
                "lane": "reviewer",
                "security_profile": "trusted_unattended",
                "artifact_dir": "/tmp/artifacts/acme_proj_3/1",
                "last_event_at": "2026-05-08T00:02:00+00:00",
            },
            {
                "issue_identifier": "acme/proj#4",
                "terminal_state": "failed",
                "task_outcome": "incomplete_permission_denied",
                "outcome_decided_by": "detector",
                "permission_denials_count": 1,
                "task_evidence": [
                    {
                        "type": "permission_denied",
                        "denials_count": 1,
                        "tool_names": ["AskUserQuestion"],
                    }
                ],
                "no_pr_reason": "needs maintainer decision",
                "provider_session_id": "provider-4",
                "security_profile": "restricted",
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
    assert "trusted_unattended" in html
    assert "acceptEdits" in html
    assert "acme/proj#1" in html
    assert "provider-1" in html
    assert "implementer" in html
    assert "reviewer" in html
    assert "permission denials: 1" in html
    assert "temporary failure" in html
    assert "completed_with_pr" in html
    assert "incomplete_permission_denied" in html
    assert "permission denials: 1" in html
    assert "AskUserQuestion" in html
    assert "needs maintainer decision" in html
    assert "issue closed" in html
    assert "/runs/acme%2Fproj%231" in html
    assert "/runs/acme%2Fproj%233" in html


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


def test_run_detail_collects_active_retry_finished_and_recovery_state() -> None:
    snapshot = _snapshot()

    active = run_detail(snapshot, "acme/proj#1")
    retry = run_detail(snapshot, "acme/proj#2")
    finished = run_detail(snapshot, "acme/proj#3")
    recovered = run_detail(snapshot, "acme/proj#5")

    assert active is not None
    assert active["active_worker"]["provider_session_id"] == "provider-1"
    assert retry is not None
    assert retry["retry_state"]["last_error"] == "temporary failure"
    assert finished is not None
    assert finished["finished_run"]["task_outcome"] == "completed_with_pr"
    blocked = run_detail(snapshot, "acme/proj#4")
    assert blocked is not None
    assert blocked["finished_run"]["permission_denials_count"] == 1
    assert recovered is not None
    assert recovered["recovery_decisions"][0]["action"] == "released"
    assert run_detail(snapshot, "acme/proj#999") is None


def test_render_run_detail_html_shows_operator_debug_fields() -> None:
    html = render_run_detail_html(_snapshot(), "acme/proj#1")

    assert html is not None
    assert "Run Summary" in html
    assert "Runtime Signals" in html
    assert "provider-1" in html
    assert "/tmp/artifacts/acme_proj_1/1" in html
    assert "permission_denials" in html
    assert "Back to dashboard" in html


def test_render_run_detail_html_shows_finished_evidence() -> None:
    html = render_run_detail_html(_snapshot(), "acme/proj#4")

    assert html is not None
    assert "incomplete_permission_denied" in html
    assert "AskUserQuestion" in html
    assert "permission_denials_count" in html


def test_render_run_detail_html_escapes_payload_values() -> None:
    snapshot = _snapshot()
    snapshot["active_workers"][0]["last_event"]["payload"] = {
        "message": "<script>alert(1)</script>"
    }

    html = render_run_detail_html(snapshot, "acme/proj#1")

    assert html is not None
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
