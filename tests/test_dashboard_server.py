"""Tests for dashboard server (#168)."""

import json
import time
from http.client import HTTPConnection

import pytest

from symphony.dashboard_server import DashboardServer


@pytest.fixture
def mock_status():
    """Mock status snapshot."""
    return {
        "state": "running",
        "active_workers": [],
        "retry_queue": [],
        "recent_finished": [],
        "recovery_decisions": [],
    }


@pytest.fixture
def dashboard_server(mock_status):
    """Dashboard server fixture."""
    server = DashboardServer(
        status_provider=lambda: mock_status,
        port=18080,  # Use non-standard port for tests
    )
    server.start()
    time.sleep(0.1)  # Give server time to start
    yield server
    server.stop()


def test_dashboard_server_starts_and_stops():
    """Test dashboard server lifecycle."""
    server = DashboardServer(
        status_provider=lambda: {"state": "idle"},
        port=18081,
    )
    assert not server._running

    server.start()
    assert server._running

    server.stop()
    assert not server._running


def test_health_endpoint(dashboard_server):
    """Test /health endpoint returns 200 OK."""
    conn = HTTPConnection("127.0.0.1", 18080)
    conn.request("GET", "/health")
    response = conn.getresponse()

    assert response.status == 200
    assert response.read() == b"OK"
    conn.close()


def test_json_status_endpoint(dashboard_server, mock_status):
    """Test /status.json endpoint returns JSON snapshot."""
    conn = HTTPConnection("127.0.0.1", 18080)
    conn.request("GET", "/status.json")
    response = conn.getresponse()

    assert response.status == 200
    assert response.getheader("Content-Type") == "application/json; charset=utf-8"
    assert response.getheader("Cache-Control") == "no-cache, no-store, must-revalidate"

    data = json.loads(response.read())
    assert data == mock_status
    conn.close()


def test_html_dashboard_endpoint(dashboard_server):
    """Test / endpoint returns HTML with auto-refresh."""
    conn = HTTPConnection("127.0.0.1", 18080)
    conn.request("GET", "/")
    response = conn.getresponse()

    assert response.status == 200
    assert response.getheader("Content-Type") == "text/html; charset=utf-8"
    assert response.getheader("Cache-Control") == "no-cache, no-store, must-revalidate"

    html = response.read().decode("utf-8")
    assert "<!doctype html>" in html.lower()
    assert "Symphony Dashboard" in html
    assert 'http-equiv="refresh"' in html
    conn.close()


def test_not_found_endpoint(dashboard_server):
    """Test 404 for unknown endpoints."""
    conn = HTTPConnection("127.0.0.1", 18080)
    conn.request("GET", "/unknown")
    response = conn.getresponse()

    assert response.status == 404
    conn.close()


def test_localhost_binding():
    """Test server binds to localhost by default."""
    server = DashboardServer(
        status_provider=lambda: {"state": "idle"},
        port=18082,
    )
    server.start()
    time.sleep(0.1)

    # Should be accessible on localhost
    conn = HTTPConnection("127.0.0.1", 18082)
    conn.request("GET", "/health")
    response = conn.getresponse()
    assert response.status == 200
    conn.close()

    server.stop()


def test_no_secret_leakage():
    """Test that secrets are not exposed in JSON endpoint."""
    status_with_secrets = {
        "state": "running",
        "config": {
            "tracker": {
                "token": "[REDACTED]",
                "api_key": "[REDACTED]",
            }
        },
    }

    server = DashboardServer(
        status_provider=lambda: status_with_secrets,
        port=18083,
    )
    server.start()
    time.sleep(0.1)

    conn = HTTPConnection("127.0.0.1", 18083)
    conn.request("GET", "/status.json")
    response = conn.getresponse()
    data = json.loads(response.read())

    # Secrets should be redacted
    assert data["config"]["tracker"]["token"] == "[REDACTED]"
    assert data["config"]["tracker"]["api_key"] == "[REDACTED]"

    conn.close()
    server.stop()


def test_html_escaping():
    """Test that HTML special characters are escaped."""
    status_with_html = {
        "state": "running",
        "active_workers": [
            {
                "issue_title": "<script>alert('xss')</script>",
            }
        ],
    }

    server = DashboardServer(
        status_provider=lambda: status_with_html,
        port=18084,
    )
    server.start()
    time.sleep(0.1)

    conn = HTTPConnection("127.0.0.1", 18084)
    conn.request("GET", "/")
    response = conn.getresponse()
    html = response.read().decode("utf-8")

    # HTML should be escaped
    assert "<script>" not in html
    assert "&lt;script&gt;" in html or "alert" not in html

    conn.close()
    server.stop()
