"""GitHub tracker integration package.

The first implementation only ships a fake tracker (#7). The real GitHub
adapter lands in #8 behind the same :class:`~symphony.github.tracker.TrackerProtocol`.
"""

from symphony.github.tracker import (
    ClaimResult,
    FakeGitHubTracker,
    ReleaseResult,
    TrackerError,
    TrackerProtocol,
)

__all__ = [
    "ClaimResult",
    "FakeGitHubTracker",
    "ReleaseResult",
    "TrackerError",
    "TrackerProtocol",
]
