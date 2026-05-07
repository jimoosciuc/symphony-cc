"""Typed data models that cross Symphony's layer boundaries.

Only the models needed by the workflow + config layer (issue #5) live here
today. Other models from `docs/IMPLEMENTATION_PLAYBOOK.md` (`Workspace`,
`SessionRecord`, `AgentEvent`, `RunArtifact`, `RetryState`) will be added by
the issues that introduce them, to keep this file from growing into a
catch-all module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
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
