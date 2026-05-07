"""GitHub tracker integration package.

The package ships:

- :class:`TrackerProtocol` — the boundary the orchestrator depends on.
- :class:`GitHubTracker` — real REST adapter (#8).
- :class:`FakeGitHubTracker` — in-memory adapter for orchestrator tests (#7).
- :class:`GitHubClient` — thin httpx-based REST client used by the real adapter.
- ``find_linked_pull_requests`` — read-side PR coordination helper (#8).
"""

from symphony.github.client import (
    GitHubClaimConflict,
    GitHubClient,
    GitHubError,
    GitHubMalformedResponse,
    GitHubMissingToken,
    GitHubNotFound,
    GitHubPermissionDenied,
    GitHubRateLimited,
    GitHubTransportError,
)
from symphony.github.pr import expected_branch_name, find_linked_pull_requests
from symphony.github.tracker import (
    ClaimResult,
    FakeGitHubTracker,
    GitHubTracker,
    ReleaseResult,
    TrackerError,
    TrackerProtocol,
)

__all__ = [
    "ClaimResult",
    "FakeGitHubTracker",
    "GitHubClaimConflict",
    "GitHubClient",
    "GitHubError",
    "GitHubMalformedResponse",
    "GitHubMissingToken",
    "GitHubNotFound",
    "GitHubPermissionDenied",
    "GitHubRateLimited",
    "GitHubTracker",
    "GitHubTransportError",
    "ReleaseResult",
    "TrackerError",
    "TrackerProtocol",
    "expected_branch_name",
    "find_linked_pull_requests",
]
