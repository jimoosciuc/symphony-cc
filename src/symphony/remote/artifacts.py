"""Artifact collection helpers for remote worker execution tests."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from symphony.artifacts import redact


@dataclass(frozen=True, slots=True)
class ArtifactCollectionResult:
    local_root: Path
    copied: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()

    @property
    def partial(self) -> bool:
        return bool(self.missing or self.errors)


@dataclass(slots=True)
class RemoteArtifactCollector:
    """Copies fake remote artifacts into the local artifact store.

    The remote worker should already write redacted artifacts. The collector
    still redacts again before local writes as defense in depth.
    """

    artifact_store: Path
    redact_keys: tuple[str, ...]
    required_files: tuple[str, ...] = (
        "events.jsonl",
        "request.json",
        "session.json",
        "terminal.json",
    )

    def collect(
        self,
        remote_root: Path,
        *,
        owner: str,
        repo: str,
        issue_number: int,
        attempt: int,
    ) -> ArtifactCollectionResult:
        local_root = self.artifact_store / f"{owner}_{repo}_{issue_number}" / str(attempt)
        local_root.mkdir(parents=True, exist_ok=True)

        copied: list[str] = []
        missing: list[str] = []
        errors: list[str] = []
        for name in self.required_files:
            source = remote_root / name
            target = local_root / name
            if not source.exists():
                missing.append(name)
                continue
            try:
                _copy_redacted(source, target, redact_keys=self.redact_keys)
                copied.append(name)
            except OSError as exc:
                errors.append(f"{name}: {exc}")
        return ArtifactCollectionResult(
            local_root=local_root,
            copied=tuple(copied),
            missing=tuple(missing),
            errors=tuple(errors),
        )


def _copy_redacted(source: Path, target: Path, *, redact_keys: tuple[str, ...]) -> None:
    text = source.read_text(encoding="utf-8")
    if source.suffix == ".json":
        payload: Any = json.loads(text)
        redacted = redact(payload, redact_keys=redact_keys)
        target.write_text(
            json.dumps(redacted, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        return

    redacted_text = redact(text, redact_keys=redact_keys)
    target.write_text(str(redacted_text), encoding="utf-8")
