"""Pure role workflow graph helpers.

This module deliberately has no GitHub API or orchestrator dependency. It
turns the typed role graph config into deterministic state-resolution and
transition-planning results that later layers can execute.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from symphony.config import (
    GitHubConfig,
    RoleConfig,
    RoleGraphConfig,
    RoleStateConfig,
    RoleTransitionConfig,
    WorkflowConfig,
)
from symphony.models import Issue


@dataclass(frozen=True, slots=True)
class StateResolution:
    """Resolved role-graph state for one issue snapshot."""

    state: RoleStateConfig | None
    matched_labels: tuple[str, ...] = ()
    ambiguous_states: tuple[str, ...] = ()
    candidate_roles: tuple[str, ...] = ()
    dispatch_role: RoleConfig | None = None
    actor: str | None = None
    gate_owner: str | None = None
    dispatchable: bool = False
    waiting: bool = False
    terminal: bool = False
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class TransitionPlan:
    """A validated label transition plan.

    The tracker layer owns actually applying these labels. The pure layer
    only computes what must change and what audit evidence was required.
    """

    transition: RoleTransitionConfig
    role: RoleConfig
    from_state: RoleStateConfig
    to_state: RoleStateConfig
    labels_to_remove: tuple[str, ...]
    labels_to_add: tuple[str, ...]
    required_evidence: tuple[str, ...]
    provided_evidence: tuple[str, ...]
    next_actor: str | None = None
    next_role: str | None = None
    gate_owner: str | None = None


@dataclass(frozen=True, slots=True)
class TransitionError:
    """Structured validation failure for a requested transition."""

    code: str
    message: str
    role: str | None = None
    transition: str | None = None
    state: str | None = None


def graph_for_config(config: WorkflowConfig) -> RoleGraphConfig:
    """Return the configured role graph or a compatibility graph.

    Existing single-lane workflows do not provide ``roles``/``states``. For
    pure role-graph consumers, expose their current labels through a small
    compatibility graph so state resolution can still reason about
    ready/running/blocked/done without changing runtime behavior.
    """

    if config.role_graph is not None:
        return config.role_graph
    return compatibility_graph(config.github)


def compatibility_graph(github: GitHubConfig | None = None) -> RoleGraphConfig:
    github = github or GitHubConfig()
    roles = {
        "implementer": RoleConfig(
            name="implementer",
            actor="agent",
            provider="claude_code",
            can_claim=("ready_impl",),
            claim_state="implementing",
            transitions=("operator_blocked", "done"),
        )
    }
    states = {
        "ready_impl": RoleStateConfig(
            name="ready_impl",
            labels=(github.ready_label,),
        ),
        "implementing": RoleStateConfig(
            name="implementing",
            labels=(github.claim_label,),
        ),
        "blocked_operator": RoleStateConfig(
            name="blocked_operator",
            labels=(github.blocked_label,),
            gate_owner="implementer",
        ),
        "done": RoleStateConfig(
            name="done",
            labels=(github.done_label,),
            terminal=True,
        ),
    }
    transitions = {
        "operator_blocked": RoleTransitionConfig(
            name="operator_blocked",
            role="implementer",
            from_states=("implementing",),
            to_state="blocked_operator",
            requires=("issue_comment",),
        ),
        "done": RoleTransitionConfig(
            name="done",
            role="implementer",
            from_states=("implementing",),
            to_state="done",
            requires=("none",),
        ),
    }
    return RoleGraphConfig(
        roles=roles,
        states=states,
        transitions=transitions,
        compatibility_mode=True,
    )


def resolve_issue_state(graph: RoleGraphConfig, issue: Issue) -> StateResolution:
    labels = {_normalize_label(label) for label in issue.labels}
    matches: list[tuple[RoleStateConfig, tuple[str, ...]]] = []
    for state in graph.states.values():
        matched = tuple(label for label in state.labels if _normalize_label(label) in labels)
        if matched:
            matches.append((state, matched))

    if not matches:
        return StateResolution(
            state=None,
            reason="no_matching_state",
        )
    if len(matches) > 1:
        return StateResolution(
            state=None,
            matched_labels=tuple(label for _state, matched in matches for label in matched),
            ambiguous_states=tuple(state.name for state, _matched in matches),
            reason="ambiguous_state",
        )

    state, matched_labels = matches[0]
    candidate_roles = tuple(
        role.name for role in graph.roles.values() if state.name in role.can_claim
    )
    dispatch_role = graph.roles.get(candidate_roles[0]) if candidate_roles else None
    actor = dispatch_role.actor if dispatch_role else None
    gate_owner = state.gate_owner
    terminal = state.terminal
    dispatchable = bool(dispatch_role and actor in {"agent", "hybrid"} and not terminal)
    waiting = terminal is False and not dispatchable
    reason = _resolution_reason(
        terminal=terminal,
        dispatch_role=dispatch_role,
        actor=actor,
        gate_owner=gate_owner,
    )
    return StateResolution(
        state=state,
        matched_labels=matched_labels,
        candidate_roles=candidate_roles,
        dispatch_role=dispatch_role,
        actor=actor,
        gate_owner=gate_owner,
        dispatchable=dispatchable,
        waiting=waiting,
        terminal=terminal,
        reason=reason,
    )


def plan_transition(
    graph: RoleGraphConfig,
    *,
    role_name: str,
    transition_name: str,
    from_state_name: str,
    evidence: Iterable[str] = (),
) -> TransitionPlan | TransitionError:
    role = graph.roles.get(role_name)
    if role is None:
        return TransitionError(
            code="unknown_role",
            message=f"unknown role {role_name!r}",
            role=role_name,
            transition=transition_name,
            state=from_state_name,
        )
    transition = graph.transitions.get(transition_name)
    if transition is None:
        return TransitionError(
            code="unknown_transition",
            message=f"unknown transition {transition_name!r}",
            role=role_name,
            transition=transition_name,
            state=from_state_name,
        )
    if transition.role != role.name:
        return TransitionError(
            code="transition_role_mismatch",
            message=(
                f"transition {transition_name!r} belongs to role {transition.role!r}, "
                f"not {role.name!r}"
            ),
            role=role_name,
            transition=transition_name,
            state=from_state_name,
        )
    from_state = graph.states.get(from_state_name)
    if from_state is None:
        return TransitionError(
            code="unknown_source_state",
            message=f"unknown source state {from_state_name!r}",
            role=role_name,
            transition=transition_name,
            state=from_state_name,
        )
    if from_state.name not in transition.from_states:
        return TransitionError(
            code="source_state_not_allowed",
            message=(
                f"transition {transition_name!r} cannot run from state "
                f"{from_state.name!r}"
            ),
            role=role_name,
            transition=transition_name,
            state=from_state_name,
        )
    to_state = graph.states.get(transition.to_state)
    if to_state is None:
        return TransitionError(
            code="unknown_destination_state",
            message=f"unknown destination state {transition.to_state!r}",
            role=role_name,
            transition=transition_name,
            state=from_state_name,
        )

    provided = tuple(dict.fromkeys(evidence))
    required = tuple(req for req in transition.requires if req != "none")
    missing = tuple(req for req in required if req not in provided)
    if missing:
        return TransitionError(
            code="missing_evidence",
            message=f"missing required evidence: {', '.join(missing)}",
            role=role_name,
            transition=transition_name,
            state=from_state_name,
        )

    next_role = _next_role_for_state(graph, to_state)
    next_actor = graph.roles[next_role].actor if next_role else None
    return TransitionPlan(
        transition=transition,
        role=role,
        from_state=from_state,
        to_state=to_state,
        labels_to_remove=from_state.labels,
        labels_to_add=to_state.labels,
        required_evidence=required,
        provided_evidence=provided,
        next_actor=next_actor,
        next_role=next_role,
        gate_owner=to_state.gate_owner,
    )


def _next_role_for_state(graph: RoleGraphConfig, state: RoleStateConfig) -> str | None:
    if state.gate_owner:
        return state.gate_owner
    for role in graph.roles.values():
        if state.name in role.can_claim:
            return role.name
    return None


def _resolution_reason(
    *,
    terminal: bool,
    dispatch_role: RoleConfig | None,
    actor: str | None,
    gate_owner: str | None,
) -> str:
    if terminal:
        return "terminal"
    if dispatch_role and actor in {"agent", "hybrid"}:
        return "dispatchable"
    if dispatch_role and actor == "human":
        return "waiting_for_human"
    if gate_owner:
        return "waiting_for_gate_owner"
    return "no_dispatch_role"


def _normalize_label(label: str) -> str:
    return label.strip().lower()
