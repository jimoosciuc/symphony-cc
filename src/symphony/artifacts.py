"""Per-attempt artifact writer.

Every Symphony attempt owns a directory under
``<claude.artifact_store>/<owner>_<repo>_<n>/<attempt>/`` (SPEC §17).
The provider and orchestrator write JSON / JSONL files there. This
module gives both layers one writer that:

- Creates the artifact directory on demand.
- Appends normalized :class:`~symphony.events.AgentEvent`s to
  ``events.jsonl`` line-by-line, redacting secrets first.
- Snapshots arbitrary JSON-serializable records to named files
  (``request.json``, ``session.json``, ``terminal.json``, ``usage.json``).

Redaction is recursive over dicts/lists. Keys named in ``redact_keys``
have their values replaced wholesale; values matching common token shapes
(``ghp_…``, ``ghs_…``, base64-ish blobs >= 32 chars) are also masked even
if their key was not in the deny list.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, is_dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from symphony.events import AgentEvent

REDACTED = "<redacted>"

# Token shapes Symphony has been bitten by. Keep narrow — false positives
# in event payloads are noisy.
_TOKEN_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"gh[psaur]_[A-Za-z0-9]{20,}"),  # GitHub tokens
    re.compile(r"sk-[A-Za-z0-9-_]{20,}"),  # OpenAI-style
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{20,}"),  # Slack-style
)


def redact(value: Any, *, redact_keys: tuple[str, ...]) -> Any:
    """Return a deep copy of ``value`` with secrets masked.

    ``redact_keys`` is matched case-insensitively against dict keys at
    every depth.
    """
    deny = frozenset(k.lower() for k in redact_keys)
    return _redact_inner(value, deny=deny)


def _redact_inner(value: Any, *, deny: frozenset[str]) -> Any:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            if isinstance(k, str) and k.lower() in deny:
                out[k] = REDACTED
            else:
                out[k] = _redact_inner(v, deny=deny)
        return out
    if isinstance(value, list):
        return [_redact_inner(v, deny=deny) for v in value]
    if isinstance(value, tuple):
        return tuple(_redact_inner(v, deny=deny) for v in value)
    if isinstance(value, str):
        return _redact_string(value)
    return value


def _looks_like_token(value: str) -> bool:
    return any(p.fullmatch(value) for p in _TOKEN_PATTERNS)


def _redact_string(value: str) -> str:
    if _looks_like_token(value):
        return REDACTED
    redacted = value
    for pattern in _TOKEN_PATTERNS:
        redacted = pattern.sub(REDACTED, redacted)
    return redacted


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, set | frozenset):
        return sorted(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


class ArtifactWriter:
    """Owns one per-attempt artifact directory.

    Construction is cheap; no I/O until the first ``write_*`` call. That
    keeps tests that only inspect paths fast and lets the orchestrator
    decide whether to materialize a directory for a worker that may not
    actually start (e.g., claim conflict).
    """

    def __init__(self, root: Path, redact_keys: tuple[str, ...]) -> None:
        self.root = root
        self.redact_keys = redact_keys

    @classmethod
    def for_attempt(
        cls,
        artifact_store: Path,
        *,
        owner: str,
        repo: str,
        issue_number: int,
        attempt: int,
        redact_keys: tuple[str, ...],
    ) -> ArtifactWriter:
        path = Path(artifact_store) / f"{owner}_{repo}_{issue_number}" / str(attempt)
        return cls(path, redact_keys=redact_keys)

    # -- I/O ---------------------------------------------------------------

    def ensure_root(self) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        return self.root

    def write_json(self, name: str, payload: Any) -> Path:
        """Write ``payload`` as redacted JSON to ``self.root / name``.

        Used for ``request.json``, ``session.json``, ``terminal.json``,
        ``usage.json``. Caller is responsible for choosing the filename.
        """
        self.ensure_root()
        target = self.root / name
        redacted = redact(payload, redact_keys=self.redact_keys)
        target.write_text(
            json.dumps(redacted, default=_json_default, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        return target

    def append_event(self, event: AgentEvent) -> Path:
        """Append one event to ``events.jsonl`` after redaction.

        Returns the path to ``events.jsonl`` so callers can log it once.
        """
        self.ensure_root()
        target = self.root / "events.jsonl"
        record = {
            "event": event.event,
            "timestamp": event.timestamp,
            "session_id": event.session_id,
            "provider": event.provider,
            "provider_session_id": event.provider_session_id,
            "issue_identifier": event.issue_identifier,
            "attempt": event.attempt,
            "payload": event.payload,
        }
        redacted = redact(record, redact_keys=self.redact_keys)
        with target.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(redacted, default=_json_default, sort_keys=True))
            fh.write("\n")
        return target
