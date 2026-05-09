"""Localhost dashboard server for live operator visibility (#168).

Serves the read-only status snapshot as JSON and HTML with auto-refresh.
Binds to localhost by default. Does not mutate daemon state or require
a database.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
from typing import Any

from symphony.dashboard import render_dashboard_html

logger = logging.getLogger(__name__)

_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 8080
_REFRESH_INTERVAL_MS = 5000  # 5 seconds


class DashboardServer:
    """Localhost HTTP server for live dashboard.

    Serves:
    - GET / → HTML dashboard with auto-refresh
    - GET /status.json → JSON status snapshot
    - GET /health → 200 OK (for monitoring)

    The server runs in a background thread and polls the status snapshot
    provider at regular intervals.
    """

    def __init__(
        self,
        status_provider: Callable[[], dict[str, Any]],
        host: str = _DEFAULT_HOST,
        port: int = _DEFAULT_PORT,
        refresh_interval_ms: int = _REFRESH_INTERVAL_MS,
    ) -> None:
        """Initialize dashboard server.

        Args:
            status_provider: Callable that returns current status snapshot
            host: Host to bind to (default: 127.0.0.1)
            port: Port to bind to (default: 8080)
            refresh_interval_ms: Refresh interval for auto-refresh (default: 5000ms)
        """
        self._status_provider = status_provider
        self._host = host
        self._port = port
        self._refresh_interval_ms = refresh_interval_ms
        self._server: HTTPServer | None = None
        self._thread: Thread | None = None
        self._running = False

    def start(self) -> None:
        """Start the dashboard server in a background thread."""
        if self._running:
            logger.warning("Dashboard server already running")
            return

        handler = self._make_handler()
        self._server = HTTPServer((self._host, self._port), handler)
        self._running = True
        self._thread = Thread(target=self._serve, daemon=True)
        self._thread.start()
        logger.info(f"Dashboard server started at http://{self._host}:{self._port}")

    def stop(self) -> None:
        """Stop the dashboard server."""
        if not self._running:
            return

        self._running = False
        if self._server:
            self._server.shutdown()
            self._server = None
        if self._thread:
            self._thread.join(timeout=5.0)
            self._thread = None
        logger.info("Dashboard server stopped")

    def _serve(self) -> None:
        """Serve requests in background thread."""
        if self._server:
            self._server.serve_forever()

    def _make_handler(self) -> type[BaseHTTPRequestHandler]:
        """Create request handler with access to status provider."""
        status_provider = self._status_provider
        refresh_interval_ms = self._refresh_interval_ms

        class DashboardHandler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                """Suppress default HTTP logging."""
                pass

            def do_GET(self) -> None:
                """Handle GET requests."""
                if self.path == "/":
                    self._serve_html()
                elif self.path == "/status.json":
                    self._serve_json()
                elif self.path == "/health":
                    self._serve_health()
                else:
                    self.send_error(404, "Not Found")

            def _serve_html(self) -> None:
                """Serve HTML dashboard with auto-refresh."""
                try:
                    snapshot = status_provider()
                    html = render_dashboard_html(snapshot)
                    # Inject auto-refresh meta tag
                    refresh_seconds = refresh_interval_ms // 1000
                    refresh_tag = f'<meta http-equiv="refresh" content="{refresh_seconds}">'
                    html = html.replace("</head>", f"{refresh_tag}\n</head>")

                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
                    self.end_headers()
                    self.wfile.write(html.encode("utf-8"))
                except Exception as e:
                    logger.exception("Error serving HTML dashboard")
                    self.send_error(500, f"Internal Server Error: {e}")

            def _serve_json(self) -> None:
                """Serve JSON status snapshot."""
                try:
                    snapshot = status_provider()
                    json_data = json.dumps(snapshot, indent=2)

                    self.send_response(200)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
                    self.end_headers()
                    self.wfile.write(json_data.encode("utf-8"))
                except Exception as e:
                    logger.exception("Error serving JSON status")
                    self.send_error(500, f"Internal Server Error: {e}")

            def _serve_health(self) -> None:
                """Serve health check endpoint."""
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"OK")

        return DashboardHandler
