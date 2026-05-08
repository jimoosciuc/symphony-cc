"""Remote worker dispatch request schema and loading.

Defines the contract between coordinator and remote worker for dispatch requests.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class DispatchRequest:
    """Remote worker dispatch request.

    Contains all information needed for a remote worker to execute a task:
    issue identification, workspace/artifact paths, and task context.
    """

    # Issue identification
    owner: str
    repo: str
    issue_number: int
    attempt: int

    # Workspace and artifact paths
    workspace_path: str
    artifact_path: str

    # Git context (for future workspace checkout)
    branch: str | None = None
    base_branch: str | None = None

    # Task context (placeholder for future prompt handling)
    prompt_ref: str | None = None

    @property
    def issue_identifier(self) -> str:
        """Return issue identifier in owner/repo#number format."""
        return f"{self.owner}/{self.repo}#{self.issue_number}"


def load_dispatch_request(path: Path) -> DispatchRequest:
    """Load and validate dispatch request from JSON file.

    Args:
        path: Path to dispatch request JSON file

    Returns:
        Validated DispatchRequest

    Raises:
        FileNotFoundError: If file doesn't exist
        ValueError: If JSON is malformed or validation fails
    """
    if not path.exists():
        raise FileNotFoundError(f"Dispatch request file not found: {path}")

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError(f"Malformed JSON in dispatch request: {e}") from e

    if not isinstance(raw, dict):
        raise ValueError(f"Dispatch request must be a JSON object, got {type(raw).__name__}")

    # Validate required fields
    required_fields = {
        "owner": str,
        "repo": str,
        "issue_number": int,
        "attempt": int,
        "workspace_path": str,
        "artifact_path": str,
    }

    missing = []
    invalid_types = []

    for field, expected_type in required_fields.items():
        if field not in raw:
            missing.append(field)
        elif (
            expected_type is int
            and (isinstance(raw[field], bool) or not isinstance(raw[field], int))
        ) or (expected_type is not int and not isinstance(raw[field], expected_type)):
            invalid_types.append(
                f"{field} must be {expected_type.__name__}, got {type(raw[field]).__name__}"
            )

    if missing:
        raise ValueError(f"Dispatch request missing required fields: {', '.join(missing)}")

    if invalid_types:
        raise ValueError(f"Dispatch request field type errors: {'; '.join(invalid_types)}")

    # Validate field values
    if raw["issue_number"] < 1:
        raise ValueError("issue_number must be >= 1")

    if raw["attempt"] < 1:
        raise ValueError("attempt must be >= 1")

    if not raw["owner"].strip():
        raise ValueError("owner cannot be empty")

    if not raw["repo"].strip():
        raise ValueError("repo cannot be empty")

    if not raw["workspace_path"].strip():
        raise ValueError("workspace_path cannot be empty")

    if not raw["artifact_path"].strip():
        raise ValueError("artifact_path cannot be empty")

    for field in ("branch", "base_branch", "prompt_ref"):
        if field in raw and raw[field] is not None and not isinstance(raw[field], str):
            raise ValueError(f"{field} must be str when set, got {type(raw[field]).__name__}")
        if isinstance(raw.get(field), str) and not raw[field].strip():
            raise ValueError(f"{field} cannot be empty when set")

    # Build DispatchRequest with optional fields
    return DispatchRequest(
        owner=raw["owner"],
        repo=raw["repo"],
        issue_number=raw["issue_number"],
        attempt=raw["attempt"],
        workspace_path=raw["workspace_path"],
        artifact_path=raw["artifact_path"],
        branch=raw.get("branch"),
        base_branch=raw.get("base_branch"),
        prompt_ref=raw.get("prompt_ref"),
    )


def serialize_dispatch_request(request: DispatchRequest) -> str:
    """Serialize dispatch request to JSON string.

    Args:
        request: DispatchRequest to serialize

    Returns:
        JSON string
    """
    data = {
        "owner": request.owner,
        "repo": request.repo,
        "issue_number": request.issue_number,
        "attempt": request.attempt,
        "workspace_path": request.workspace_path,
        "artifact_path": request.artifact_path,
    }

    # Include optional fields if present
    if request.branch is not None:
        data["branch"] = request.branch
    if request.base_branch is not None:
        data["base_branch"] = request.base_branch
    if request.prompt_ref is not None:
        data["prompt_ref"] = request.prompt_ref

    return json.dumps(data, indent=2)
