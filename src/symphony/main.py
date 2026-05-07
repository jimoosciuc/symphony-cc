"""Top-level program entry point for Symphony.

This is intentionally a thin re-export of :func:`symphony.cli.main` for now.
Later milestones will move the orchestrator startup wiring (config load,
artifact dirs, signal handlers) here so that ``cli.main`` stays focused on
argument parsing.
"""

from symphony.cli import main

__all__ = ["main"]
