"""Tests for remote worker dispatch request schema."""

import json
from pathlib import Path

import pytest

from symphony.remote.dispatch import (
    DispatchRequest,
    load_dispatch_request,
    serialize_dispatch_request,
)


def test_dispatch_request_issue_identifier():
    """Test DispatchRequest.issue_identifier property."""
    request = DispatchRequest(
        owner="test-owner",
        repo="test-repo",
        issue_number=42,
        attempt=1,
        workspace_path="/remote/workspaces/test-owner_test-repo_42",
        artifact_path="/remote/artifacts/test-owner_test-repo_42/1",
    )
    assert request.issue_identifier == "test-owner/test-repo#42"


def test_load_dispatch_request_valid(tmp_path: Path):
    """Test loading valid dispatch request."""
    dispatch_file = tmp_path / "dispatch.json"
    dispatch_file.write_text(
        json.dumps(
            {
                "owner": "test-owner",
                "repo": "test-repo",
                "issue_number": 42,
                "attempt": 1,
                "workspace_path": "/remote/workspaces/test-owner_test-repo_42",
                "artifact_path": "/remote/artifacts/test-owner_test-repo_42/1",
            }
        ),
        encoding="utf-8",
    )

    request = load_dispatch_request(dispatch_file)

    assert request.owner == "test-owner"
    assert request.repo == "test-repo"
    assert request.issue_number == 42
    assert request.attempt == 1
    assert request.workspace_path == "/remote/workspaces/test-owner_test-repo_42"
    assert request.artifact_path == "/remote/artifacts/test-owner_test-repo_42/1"
    assert request.branch is None
    assert request.base_branch is None
    assert request.prompt_ref is None


def test_load_dispatch_request_with_optional_fields(tmp_path: Path):
    """Test loading dispatch request with optional fields."""
    dispatch_file = tmp_path / "dispatch.json"
    dispatch_file.write_text(
        json.dumps(
            {
                "owner": "test-owner",
                "repo": "test-repo",
                "issue_number": 42,
                "attempt": 2,
                "workspace_path": "/remote/workspaces/test-owner_test-repo_42",
                "artifact_path": "/remote/artifacts/test-owner_test-repo_42/2",
                "branch": "feature/test",
                "base_branch": "main",
                "prompt_ref": "prompt://coordinator/123",
            }
        ),
        encoding="utf-8",
    )

    request = load_dispatch_request(dispatch_file)

    assert request.branch == "feature/test"
    assert request.base_branch == "main"
    assert request.prompt_ref == "prompt://coordinator/123"


def test_load_dispatch_request_missing_file(tmp_path: Path):
    """Test loading dispatch request from missing file."""
    dispatch_file = tmp_path / "missing.json"

    with pytest.raises(FileNotFoundError, match="Dispatch request file not found"):
        load_dispatch_request(dispatch_file)


def test_load_dispatch_request_malformed_json(tmp_path: Path):
    """Test loading dispatch request with malformed JSON."""
    dispatch_file = tmp_path / "malformed.json"
    dispatch_file.write_text("{invalid json", encoding="utf-8")

    with pytest.raises(ValueError, match="Malformed JSON in dispatch request"):
        load_dispatch_request(dispatch_file)


def test_load_dispatch_request_not_object(tmp_path: Path):
    """Test loading dispatch request that is not a JSON object."""
    dispatch_file = tmp_path / "not-object.json"
    dispatch_file.write_text('["array", "not", "object"]', encoding="utf-8")

    with pytest.raises(ValueError, match="must be a JSON object"):
        load_dispatch_request(dispatch_file)


def test_load_dispatch_request_missing_required_fields(tmp_path: Path):
    """Test loading dispatch request with missing required fields."""
    dispatch_file = tmp_path / "missing-fields.json"
    dispatch_file.write_text(
        json.dumps(
            {
                "owner": "test-owner",
                "repo": "test-repo",
                # Missing issue_number, attempt, workspace_path, artifact_path
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="missing required fields"):
        load_dispatch_request(dispatch_file)


def test_load_dispatch_request_invalid_field_types(tmp_path: Path):
    """Test loading dispatch request with invalid field types."""
    dispatch_file = tmp_path / "invalid-types.json"
    dispatch_file.write_text(
        json.dumps(
            {
                "owner": "test-owner",
                "repo": "test-repo",
                "issue_number": "not-an-int",  # Should be int
                "attempt": 1,
                "workspace_path": "/remote/workspaces/test",
                "artifact_path": "/remote/artifacts/test",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="field type errors"):
        load_dispatch_request(dispatch_file)


def test_load_dispatch_request_invalid_issue_number(tmp_path: Path):
    """Test loading dispatch request with invalid issue_number."""
    dispatch_file = tmp_path / "invalid-issue.json"
    dispatch_file.write_text(
        json.dumps(
            {
                "owner": "test-owner",
                "repo": "test-repo",
                "issue_number": 0,  # Must be >= 1
                "attempt": 1,
                "workspace_path": "/remote/workspaces/test",
                "artifact_path": "/remote/artifacts/test",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="issue_number must be >= 1"):
        load_dispatch_request(dispatch_file)


def test_load_dispatch_request_invalid_attempt(tmp_path: Path):
    """Test loading dispatch request with invalid attempt."""
    dispatch_file = tmp_path / "invalid-attempt.json"
    dispatch_file.write_text(
        json.dumps(
            {
                "owner": "test-owner",
                "repo": "test-repo",
                "issue_number": 1,
                "attempt": 0,  # Must be >= 1
                "workspace_path": "/remote/workspaces/test",
                "artifact_path": "/remote/artifacts/test",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="attempt must be >= 1"):
        load_dispatch_request(dispatch_file)


def test_load_dispatch_request_empty_owner(tmp_path: Path):
    """Test loading dispatch request with empty owner."""
    dispatch_file = tmp_path / "empty-owner.json"
    dispatch_file.write_text(
        json.dumps(
            {
                "owner": "  ",  # Empty after strip
                "repo": "test-repo",
                "issue_number": 1,
                "attempt": 1,
                "workspace_path": "/remote/workspaces/test",
                "artifact_path": "/remote/artifacts/test",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="owner cannot be empty"):
        load_dispatch_request(dispatch_file)


def test_serialize_dispatch_request_minimal():
    """Test serializing dispatch request with only required fields."""
    request = DispatchRequest(
        owner="test-owner",
        repo="test-repo",
        issue_number=42,
        attempt=1,
        workspace_path="/remote/workspaces/test",
        artifact_path="/remote/artifacts/test",
    )

    serialized = serialize_dispatch_request(request)
    data = json.loads(serialized)

    assert data["owner"] == "test-owner"
    assert data["repo"] == "test-repo"
    assert data["issue_number"] == 42
    assert data["attempt"] == 1
    assert data["workspace_path"] == "/remote/workspaces/test"
    assert data["artifact_path"] == "/remote/artifacts/test"
    assert "branch" not in data
    assert "base_branch" not in data
    assert "prompt_ref" not in data


def test_serialize_dispatch_request_with_optional_fields():
    """Test serializing dispatch request with optional fields."""
    request = DispatchRequest(
        owner="test-owner",
        repo="test-repo",
        issue_number=42,
        attempt=1,
        workspace_path="/remote/workspaces/test",
        artifact_path="/remote/artifacts/test",
        branch="feature/test",
        base_branch="main",
        prompt_ref="prompt://coordinator/123",
    )

    serialized = serialize_dispatch_request(request)
    data = json.loads(serialized)

    assert data["branch"] == "feature/test"
    assert data["base_branch"] == "main"
    assert data["prompt_ref"] == "prompt://coordinator/123"


def test_serialize_and_load_roundtrip(tmp_path: Path):
    """Test serialize and load roundtrip."""
    original = DispatchRequest(
        owner="test-owner",
        repo="test-repo",
        issue_number=42,
        attempt=3,
        workspace_path="/remote/workspaces/test",
        artifact_path="/remote/artifacts/test",
        branch="feature/test",
    )

    serialized = serialize_dispatch_request(original)
    dispatch_file = tmp_path / "dispatch.json"
    dispatch_file.write_text(serialized, encoding="utf-8")

    loaded = load_dispatch_request(dispatch_file)

    assert loaded.owner == original.owner
    assert loaded.repo == original.repo
    assert loaded.issue_number == original.issue_number
    assert loaded.attempt == original.attempt
    assert loaded.workspace_path == original.workspace_path
    assert loaded.artifact_path == original.artifact_path
    assert loaded.branch == original.branch
