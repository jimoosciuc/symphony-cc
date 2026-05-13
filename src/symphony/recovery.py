"""Orchestrator restart recovery (issue #31, SPEC §16).

When the daemon crashes / is killed mid-flight, the active worker's OS
process is gone (per SPEC §16 *no live worker process is assumed
recoverable*) but everything Symphony persisted survives:

- the workspace directory under ``workspace.root``;
- the session record under ``claude.session_store``;
- the per-attempt artifacts under ``claude.artifact_store``;
- the GitHub claim label / claim comment on the issue.

This module reconciles those persisted bits on the next ``symphony run``.
The flow runs once at startup, before the first poll tick:

1. **Discover** every session record under ``claude.session_store`` with
   ``terminal_state is None`` — those are the records that did not reach
   a clean terminal write.
2. For each, **fetch the fresh issue state** from the tracker. If the
   issue has since closed, hit an exclude/blocked label, or vanished,
   release the stale claim and mark the record cancelled. No resume.
3. Otherwise, route by ``claude.retry_resume_policy`` (SPEC §11.2,
   docs/claude-provider.md §5.3):

   - ``resume_same_session`` → call ``provider.restore(record)`` when the
     record has a provider session id. On success the resumed worker is
     dropped into ``Orchestrator.active`` so the next ``run_once`` does
     not double-dispatch it; the worker is then driven inline through
     ``_run_worker``. If the record never captured a provider session id,
     release the claim for a fresh dispatch because there is no upstream
     conversation to resume. On other :class:`ProviderRestoreError` →
     mark blocked.
   - ``new_session_with_summary`` → release the claim and stash the
     persisted ``provider_session_id`` so the next normal dispatch
     populates ``previous_provider_session_ids`` on the fresh session
     for handoff.
   - ``fail_closed`` → mark the issue blocked. Operator must clear the
     label before another run touches it.

Every decision is written to a ``recovery.json`` artifact under the
record's attempt directory so an operator can audit what the daemon did
on its way back up.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from symphony.models import Issue
from symphony.provider.base import SessionRecord, Terminal

_LOG = logging.getLogger("symphony.recovery")


# -- Records ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RecoveryDecision:
    """Outcome of reconciling one persisted session record at startup.

    ``action`` is the verb the operator cares about; ``reason`` is
    human-readable. ``restored_session_id`` is set only when the action
    is ``"resumed"``. ``issue_number`` is carried separately so
    decisions for vanished issues (no fresh fetch) still attribute
    correctly.
    """

    record_path: Path
    issue_identifier: str
    issue_number: int
    action: str  # one of: "resumed", "released", "blocked", "discarded", "skipped"
    reason: str
    policy: str
    restored_session_id: str | None = None
    decided_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_json(self) -> dict[str, Any]:
        return {
            "record_path": str(self.record_path),
            "issue_identifier": self.issue_identifier,
            "issue_number": self.issue_number,
            "action": self.action,
            "reason": self.reason,
            "policy": self.policy,
            "restored_session_id": self.restored_session_id,
            "decided_at": self.decided_at.isoformat(),
        }


# Action constants — string form (not Enum) keeps recovery.json greppable
# without an extra serialization step.
ACTION_RESUMED = "resumed"
ACTION_RELEASED = "released"
ACTION_BLOCKED = "blocked"
ACTION_DISCARDED = "discarded"
ACTION_SKIPPED = "skipped"


# -- Discovery ---------------------------------------------------------------


def discover_persisted_records(
    session_store: Path,
) -> list[tuple[Path, SessionRecord]]:
    """Scan ``session_store`` for in-flight :class:`SessionRecord`s.

    Records with ``terminal_state`` already set are filtered out — those
    finished cleanly under the previous daemon. Records that fail to
    parse (missing fields, broken JSON) are logged and skipped so a
    single corrupt file does not block recovery for the rest.

    Returns a list of ``(path, record)`` tuples sorted by file mtime
    (oldest first). The mtime ordering matters: when two records exist
    for the same issue (rare, but possible if a prior recovery aborted
    mid-write) the orchestrator wants to act on the older snapshot last
    so the newer one survives.
    """
    if not session_store.exists():
        return []
    out: list[tuple[float, Path, SessionRecord]] = []
    for path in sorted(session_store.glob("*.json")):
        # Skip the .tmp files written by the atomic _persist_session.
        if path.suffix == ".tmp" or path.name.endswith(".json.tmp"):
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            _LOG.warning("recovery: skipping unreadable session record %s: %s", path, exc)
            continue
        try:
            record = _hydrate_record(data)
        except (KeyError, ValueError, TypeError) as exc:
            _LOG.warning("recovery: skipping malformed session record %s: %s", path, exc)
            continue
        if record.terminal_state is not None:
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = 0.0
        out.append((mtime, path, record))

    out.sort(key=lambda item: item[0])
    return [(p, r) for _, p, r in out]


def discover_workspace_records(
    session_store: Path,
) -> list[tuple[Path, SessionRecord]]:
    """Scan ``session_store`` for records that can identify workspaces.

    Unlike :func:`discover_persisted_records`, this includes terminal
    records because workspace cleanup needs to consider completed runs
    whose workspace directories are still on disk. Records are deduped
    by ``issue_identifier`` with the newest session-record mtime winning.
    """
    if not session_store.exists():
        return []
    latest: dict[str, tuple[float, Path, SessionRecord]] = {}
    for path in sorted(session_store.glob("*.json")):
        if path.suffix == ".tmp" or path.name.endswith(".json.tmp"):
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            _LOG.warning("recovery: skipping unreadable session record %s: %s", path, exc)
            continue
        try:
            record = _hydrate_record(data)
        except (KeyError, ValueError, TypeError) as exc:
            _LOG.warning("recovery: skipping malformed session record %s: %s", path, exc)
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = 0.0
        existing = latest.get(record.issue_identifier)
        if existing is None or mtime >= existing[0]:
            latest[record.issue_identifier] = (mtime, path, record)

    out = sorted(latest.values(), key=lambda item: item[0])
    return [(p, r) for _, p, r in out]


def _hydrate_record(data: dict[str, Any]) -> SessionRecord:
    """Reverse the snapshot written by ``provider.claude_code._persist_session``.

    Permissive on field absence so older record formats can still be
    loaded — the orchestrator only strictly needs ``session_id``,
    ``issue_identifier``, ``issue_number``, ``workspace_path``, and
    ``provider``. Optional fields fall back to safe defaults.
    """
    started_at_raw = data.get("started_at")
    if started_at_raw is None:
        started_at = datetime.now(timezone.utc)
    else:
        started_at = _parse_iso(started_at_raw)

    last_event_raw = data.get("last_event_at")
    last_event_at = _parse_iso(last_event_raw) if last_event_raw else None

    terminal_raw = data.get("terminal_state")
    terminal = Terminal(terminal_raw) if terminal_raw else None

    artifact_dir = Path(data.get("artifact_dir") or ".")
    workspace_path = Path(data["workspace_path"])

    session_store_raw = data.get("session_store")
    session_store = Path(session_store_raw) if session_store_raw else None

    transcript_raw = data.get("transcript_path")
    transcript_path = Path(transcript_raw) if transcript_raw else None

    return SessionRecord(
        session_id=str(data["session_id"]),
        provider=str(data["provider"]),
        issue_identifier=str(data["issue_identifier"]),
        issue_number=int(data["issue_number"]),
        workspace_path=workspace_path,
        artifact_dir=artifact_dir,
        started_at=started_at,
        attempt=int(data.get("attempt") or 1),
        provider_session_id=data.get("provider_session_id"),
        transcript_path=transcript_path,
        turn_count=int(data.get("turn_count") or 0),
        last_event_at=last_event_at,
        terminal_state=terminal,
        previous_provider_session_ids=list(data.get("previous_provider_session_ids") or []),
        session_store=session_store,
    )


def _parse_iso(raw: Any) -> datetime:
    if isinstance(raw, datetime):
        return raw
    text = str(raw)
    # ``fromisoformat`` accepts ``+00:00`` but not the ``Z`` suffix on 3.10.
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return datetime.fromisoformat(text)


# -- Eligibility -------------------------------------------------------------


def issue_is_actionable(
    issue: Issue,
    *,
    exclude_labels: Iterable[str],
    blocked_label: str,
) -> tuple[bool, str | None]:
    """Decide if an issue from the tracker is still worth resuming.

    Returns ``(actionable, reason_if_not)``. Reasons match the SPEC §16
    "stale claim" cases an operator might see in ``recovery.json``.
    """
    if issue.state != "open":
        return False, f"issue is {issue.state!r}"
    if blocked_label in issue.labels:
        return False, f"issue carries blocked label {blocked_label!r}"
    excluded = [lbl for lbl in exclude_labels if lbl in issue.labels]
    if excluded:
        return False, f"issue carries exclude labels: {sorted(excluded)}"
    return True, None
