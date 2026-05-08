"""Tests for fake remote artifact collection."""

from __future__ import annotations

import json
from pathlib import Path

from symphony.artifacts import REDACTED
from symphony.remote.artifacts import RemoteArtifactCollector


def _write_remote_artifacts(root: Path, *, include_terminal: bool = True) -> None:
    root.mkdir(parents=True)
    (root / "events.jsonl").write_text(
        '{"event":"heartbeat","token":"ghp_abcdefghijklmnopqrstuvwxyz123456"}\n',
        encoding="utf-8",
    )
    (root / "request.json").write_text(
        json.dumps({"prompt": "use sk-abcdefghijklmnopqrstuvwxyz123456"}),
        encoding="utf-8",
    )
    (root / "session.json").write_text(
        json.dumps({"provider_session_id": "session-1"}),
        encoding="utf-8",
    )
    if include_terminal:
        (root / "terminal.json").write_text(
            json.dumps({"status": "completed"}),
            encoding="utf-8",
        )


def test_artifact_collector_copies_remote_artifacts(tmp_path: Path) -> None:
    remote_root = tmp_path / "remote"
    local_store = tmp_path / "local"
    _write_remote_artifacts(remote_root)

    result = RemoteArtifactCollector(
        local_store,
        redact_keys=("token",),
    ).collect(
        remote_root,
        owner="jimoosciuc",
        repo="symphony-cc",
        issue_number=117,
        attempt=1,
    )

    assert result.partial is False
    assert set(result.copied) == {
        "events.jsonl",
        "request.json",
        "session.json",
        "terminal.json",
    }
    assert (result.local_root / "terminal.json").exists()


def test_artifact_collector_redacts_again_before_local_write(tmp_path: Path) -> None:
    remote_root = tmp_path / "remote"
    local_store = tmp_path / "local"
    _write_remote_artifacts(remote_root)

    result = RemoteArtifactCollector(
        local_store,
        redact_keys=("token",),
    ).collect(
        remote_root,
        owner="jimoosciuc",
        repo="symphony-cc",
        issue_number=117,
        attempt=1,
    )

    events_text = (result.local_root / "events.jsonl").read_text(encoding="utf-8")
    request_payload = json.loads((result.local_root / "request.json").read_text())
    assert "ghp_abcdefghijklmnopqrstuvwxyz123456" not in events_text
    assert "sk-abcdefghijklmnopqrstuvwxyz123456" not in request_payload["prompt"]
    assert REDACTED in events_text
    assert REDACTED in request_payload["prompt"]


def test_artifact_collector_reports_missing_artifacts(tmp_path: Path) -> None:
    remote_root = tmp_path / "remote"
    local_store = tmp_path / "local"
    _write_remote_artifacts(remote_root, include_terminal=False)

    result = RemoteArtifactCollector(
        local_store,
        redact_keys=("token",),
    ).collect(
        remote_root,
        owner="jimoosciuc",
        repo="symphony-cc",
        issue_number=117,
        attempt=1,
    )

    assert result.partial is True
    assert result.missing == ("terminal.json",)
    assert "events.jsonl" in result.copied
