"""Typed data models that cross Symphony's layer boundaries.

Models needed by the workflow + config layer (#5) and the workspace
manager (#6) live here today. Other models from
`docs/IMPLEMENTATION_PLAYBOOK.md` (`SessionRecord`, `AgentEvent`,
`RunArtifact`, `RetryState`) will be added by the issues that introduce
them, to keep this file from growing into a catch-all module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class Issue:
    """A normalized GitHub issue. See SPEC.md §5.1.

    Used by the workflow loader's prompt renderer and by the orchestrator's
    dispatch loop. The frozen + slotted shape prevents accidental mutation
    once an issue snapshot has been taken.
    """

    id: str
    number: int
    identifier: str
    owner: str
    repo: str
    title: str
    body: str
    state: str
    url: str
    labels: tuple[str, ...] = ()
    assignees: tuple[str, ...] = ()
    updated_at: datetime | None = None
    created_at: datetime | None = None
    raw: dict[str, Any] | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class PullRequest:
    """A normalized GitHub pull request. See SPEC.md §5.2.

    `linked_issue_identifier` is set when the PR was discovered through an
    issue's linked-PR query; the field stays `None` for PRs found by branch
    name or head_ref alone.
    """

    id: str
    number: int
    owner: str
    repo: str
    title: str
    url: str
    state: str
    head_ref: str
    base_ref: str
    is_draft: bool = False
    mergeable_state: str | None = None
    linked_issue_identifier: str | None = None
    raw: dict[str, Any] | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class Workspace:
    """A prepared per-issue workspace. See SPEC.md §5.3 and §8.

    `repo_path` defaults to `path` since the first implementation does not
    yet split repository checkouts into a sub-directory. The field is here
    so future issues that introduce git population can populate it without
    breaking callers.

    `reused` is True when the workspace directory already existed before
    `prepare()` was called. The orchestrator uses it to gate `after_create`.
    """

    issue_identifier: str
    workspace_key: str
    path: Path
    repo_path: Path
    created_at: datetime
    reused: bool
