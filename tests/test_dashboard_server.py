"""Tests for dashboard server (#168)."""

from __future__ import annotations

import json
from http.client import HTTPConnection
from typing import Any

import pytest

from symphony.dashboard_server import DashboardServer


@pytest.fixture
def mock_status() -> dict[str, Any]:
    return {
        "state": "running",
        "active_workers": [],
        "retry_queue": [],
        "recent_finished": [],
        "recovery_decisions": [],
    }


@pytest.fixture
def dashboard_server(mock_status: dict[str, Any]):
    server = DashboardServer(
        status_provider=lambda: mock_status,
        port=0,
    )
    server.start()
    yield server
    server.stop()


def _get(server: DashboardServer, path: str) -> tuple[int, dict[str, str], bytes]:
    conn = HTTPConnection(server.host, server.port)
    try:
        conn.request("GET", path)
        response = conn.getresponse()
        headers = {key: value for key, value in response.getheaders()}
        body = response.read()
        return response.status, headers, body
    finally:
        conn.close()


def test_dashboard_server_starts_and_stops() -> None:
    server = DashboardServer(
        status_provider=lambda: {"state": "idle"},
        port=0,
    )
    assert not server._running

    try:
        server.start()
        assert server._running
        assert server.host == "127.0.0.1"
        assert server.port > 0
    finally:
        server.stop()
    assert not server._running


def test_health_endpoint(dashboard_server: DashboardServer) -> None:
    status, _, body = _get(dashboard_server, "/health")

    assert status == 200
    assert body == b"OK"


def test_json_status_endpoint(
    dashboard_server: DashboardServer,
    mock_status: dict[str, Any],
) -> None:
    status, headers, body = _get(dashboard_server, "/status.json")

    assert status == 200
    assert headers["Content-Type"] == "application/json; charset=utf-8"
    assert headers["Cache-Control"] == "no-cache, no-store, must-revalidate"
    assert json.loads(body) == mock_status


def test_html_dashboard_endpoint(dashboard_server: DashboardServer) -> None:
    status, headers, body = _get(dashboard_server, "/")

    assert status == 200
    assert headers["Content-Type"] == "text/html; charset=utf-8"
    assert headers["Cache-Control"] == "no-cache, no-store, must-revalidate"

    html = body.decode("utf-8")
    assert "<!doctype html>" in html.lower()
    assert "Symphony Dashboard" in html
    assert 'http-equiv="refresh"' in html


def test_not_found_endpoint(dashboard_server: DashboardServer) -> None:
    status, _, _ = _get(dashboard_server, "/unknown")

    assert status == 404


def test_localhost_binding() -> None:
    server = DashboardServer(
        status_provider=lambda: {"state": "idle"},
        port=0,
    )
    try:
        server.start()
        assert server.host == "127.0.0.1"
        status, _, _ = _get(server, "/health")
        assert status == 200
    finally:
        server.stop()


def test_no_secret_leakage() -> None:
    raw_token = "ghp_abcdefghijklmnopqrstuvwxyz123456"
    status_with_secrets = {
        "state": "running",
        "config": {
            "tracker": {
                "token": raw_token,
                "api_key": "plain-api-key",
            }
        },
    }

    server = DashboardServer(
        status_provider=lambda: status_with_secrets,
        port=0,
    )
    try:
        server.start()
        _, _, body = _get(server, "/status.json")
        text = body.decode("utf-8")
        data = json.loads(body)

        assert raw_token not in text
        assert "plain-api-key" not in text
        assert data["config"]["tracker"]["token"] == "<redacted>"
        assert data["config"]["tracker"]["api_key"] == "<redacted>"
    finally:
        server.stop()


def test_html_escaping() -> None:
    status_with_html = {
        "state": "running",
        "active_workers": [
            {
                "issue_identifier": "<script>alert('xss')</script>",
                "issue_url": 'https://example.com/"bad"',
            }
        ],
    }

    server = DashboardServer(
        status_provider=lambda: status_with_html,
        port=0,
    )
    try:
        server.start()
        _, _, body = _get(server, "/")
        html = body.decode("utf-8")

        assert "<script>" not in html
        assert "&lt;script&gt;" in html
        assert 'href="https://example.com/&quot;bad&quot;"' in html
    finally:
        server.stop()


def test_provider_exception_does_not_leak_secret() -> None:
    secret = "ghp_abcdefghijklmnopqrstuvwxyz123456"

    def broken_status() -> dict[str, Any]:
        raise RuntimeError(f"failed with {secret}")

    server = DashboardServer(status_provider=broken_status, port=0)
    try:
        server.start()
        status, _, body = _get(server, "/status.json")
        assert status == 500
        assert secret not in body.decode("utf-8")
    finally:
        server.stop()
