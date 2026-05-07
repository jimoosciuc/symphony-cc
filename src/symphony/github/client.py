"""Thin HTTP client for the GitHub REST API.

The tracker adapter and PR coordinator both go through :class:`GitHubClient`
so that error mapping, auth, and retry behavior live in one place. The
client is sync (``httpx.Client``) because Symphony's orchestrator wraps
each tracker call in `asyncio.to_thread` — see SPEC §9.4 / docs/IMPLEMENTATION_PLAYBOOK.md.

Why httpx (not requests, not stdlib urllib):

- httpx ships a documented :class:`MockTransport` we use for unit tests
  without monkey-patching network globals.
- Same library scales up to async if a future provider needs it.
- requests is mature but doesn't have the test-transport ergonomics.
- urllib's ergonomics are bad enough that JSON POST/DELETE noise would
  swamp the real adapter logic.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import httpx

GITHUB_API_BASE = "https://api.github.com"
USER_AGENT = "symphony-cc"

# 60s default — GitHub recommends short timeouts on REST endpoints; the
# orchestrator's poll cycle is on the order of minutes so this is generous.
DEFAULT_TIMEOUT_SECONDS = 60.0


# -- Errors --------------------------------------------------------------------


class GitHubError(RuntimeError):
    """Base class for GitHub adapter errors.

    Sub-classes mirror the categories from SPEC §9.4 so the orchestrator's
    error-handling code can branch by type without parsing strings.
    """

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class GitHubMissingToken(GitHubError):
    """No authentication token was supplied to the client."""


class GitHubPermissionDenied(GitHubError):
    """403 from the API after auth — token lacks the needed scope."""


class GitHubNotFound(GitHubError):
    """404 — repo, issue, or PR does not exist."""


class GitHubRateLimited(GitHubError):
    """429 or 403 with rate-limit headers — caller should back off."""

    def __init__(
        self, message: str, *, status_code: int | None = None, retry_after: float | None = None
    ) -> None:
        super().__init__(message, status_code=status_code)
        self.retry_after = retry_after


class GitHubTransportError(GitHubError):
    """Connection refused, DNS failure, TLS handshake error, etc."""


class GitHubMalformedResponse(GitHubError):
    """200-class response whose body wasn't valid JSON or was the wrong shape."""


class GitHubClaimConflict(GitHubError):
    """422 / state conflict during a claim attempt.

    Distinct from generic GitHubError so the tracker can surface a clean
    "another run owns this issue" path without parsing 422 bodies.
    """


# -- Client --------------------------------------------------------------------


class GitHubClient:
    """Authenticated REST client for one GitHub user/installation.

    One client per Symphony run. Construction validates the token is
    non-empty so the tracker's first call fails closed instead of mid-poll.
    """

    def __init__(
        self,
        token: str,
        *,
        base_url: str = GITHUB_API_BASE,
        transport: httpx.BaseTransport | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        if not token:
            raise GitHubMissingToken("github token is empty")
        self._client = httpx.Client(
            base_url=base_url,
            transport=transport,
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": USER_AGENT,
            },
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> GitHubClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # -- HTTP verbs --------------------------------------------------------

    def get(self, path: str, *, params: Mapping[str, Any] | None = None) -> Any:
        return self._request_json("GET", path, params=params)

    def post(self, path: str, *, json_body: Any | None = None) -> Any:
        return self._request_json("POST", path, json_body=json_body)

    def patch(self, path: str, *, json_body: Any | None = None) -> Any:
        return self._request_json("PATCH", path, json_body=json_body)

    def delete(self, path: str) -> None:
        # DELETE typically returns 204 No Content; tracker doesn't need a body.
        self._request("DELETE", path, expect_json=False)

    # -- Internals ---------------------------------------------------------

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Any | None = None,
    ) -> Any:
        response = self._request(method, path, params=params, json_body=json_body, expect_json=True)
        if response is None:
            return None
        try:
            return response.json()
        except json.JSONDecodeError as exc:
            raise GitHubMalformedResponse(
                f"{method} {path} returned non-JSON body: {response.text[:200]!r}",
                status_code=response.status_code,
            ) from exc

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Any | None = None,
        expect_json: bool = True,
    ) -> httpx.Response | None:
        try:
            response = self._client.request(method, path, params=params, json=json_body)
        except httpx.TimeoutException as exc:
            raise GitHubTransportError(f"{method} {path} timed out") from exc
        except httpx.TransportError as exc:
            raise GitHubTransportError(f"{method} {path} transport error: {exc}") from exc

        if response.status_code in {200, 201, 202, 204}:
            return response if expect_json else None
        self._raise_for_status(method, path, response)
        return None  # pragma: no cover - _raise_for_status always raises

    def _raise_for_status(self, method: str, path: str, response: httpx.Response) -> None:
        status = response.status_code
        # GitHub's secondary-rate-limit signal: 403 with x-ratelimit-remaining=0
        # or 429.
        rate_limited = status == 429 or (
            status == 403 and response.headers.get("x-ratelimit-remaining") == "0"
        )
        if rate_limited:
            retry_after_raw = response.headers.get("retry-after")
            try:
                retry_after = float(retry_after_raw) if retry_after_raw else None
            except ValueError:
                retry_after = None
            raise GitHubRateLimited(
                f"{method} {path} rate limited (status={status})",
                status_code=status,
                retry_after=retry_after,
            )
        if status == 401:
            raise GitHubMissingToken(f"{method} {path}: 401 Unauthorized")
        if status == 403:
            raise GitHubPermissionDenied(f"{method} {path}: 403 Forbidden")
        if status == 404:
            raise GitHubNotFound(f"{method} {path}: 404 Not Found")
        if status == 422:
            raise GitHubClaimConflict(
                f"{method} {path}: 422 Unprocessable Entity ({_summary(response)})",
                status_code=status,
            )
        raise GitHubError(
            f"{method} {path}: unexpected {status} {_summary(response)}",
            status_code=status,
        )


def _summary(response: httpx.Response) -> str:
    """Short, log-safe summary of a non-success response body.

    Truncates to 200 chars so a paginated error blob doesn't fill the log.
    """
    body = response.text
    if len(body) > 200:
        body = body[:197] + "..."
    return body or "(empty body)"
