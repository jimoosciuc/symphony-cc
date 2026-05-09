from __future__ import annotations

from pathlib import Path

from symphony.config import RoleGraphConfig, build_config
from symphony.models import Issue
from symphony.role_graph import (
    StateResolution,
    TransitionError,
    TransitionPlan,
    graph_for_config,
    plan_claim,
    plan_reverse_claim,
    plan_transition,
    resolve_issue_state,
)


def test_resolves_dispatchable_implementer_state() -> None:
    graph = _graph()
    result = resolve_issue_state(graph, _issue(labels=("symphony-ready-impl",)))

    assert result.state is not None
    assert result.state.name == "ready_impl"
    assert result.dispatchable is True
    assert result.waiting is False
    assert result.dispatch_role is not None
    assert result.dispatch_role.name == "implementer"
    assert result.actor == "agent"
    assert result.reason == "dispatchable"


def test_resolves_human_review_state_as_waiting() -> None:
    graph = _graph()
    result = resolve_issue_state(graph, _issue(labels=("symphony-ready-review",)))

    assert result.state is not None
    assert result.state.name == "ready_review"
    assert result.dispatchable is False
    assert result.waiting is True
    assert result.dispatch_role is not None
    assert result.dispatch_role.name == "reviewer"
    assert result.actor == "human"
    assert result.reason == "waiting_for_human"


def test_resolves_gate_owner_state_as_waiting() -> None:
    graph = _graph()
    result = resolve_issue_state(graph, _issue(labels=("symphony-needs-design",)))

    assert result.state is not None
    assert result.state.name == "needs_design"
    assert result.dispatchable is True
    assert result.waiting is False
    assert result.dispatch_role is not None
    assert result.dispatch_role.name == "leader"
    assert result.actor == "hybrid"
    assert result.gate_owner == "leader"


def test_resolves_terminal_state_as_not_dispatchable() -> None:
    graph = _graph()
    result = resolve_issue_state(graph, _issue(labels=("symphony-done",)))

    assert result.state is not None
    assert result.state.name == "done"
    assert result.terminal is True
    assert result.dispatchable is False
    assert result.waiting is False
    assert result.reason == "terminal"


def test_unknown_state_has_structured_reason() -> None:
    graph = _graph()
    result = resolve_issue_state(graph, _issue(labels=("unrelated",)))

    assert result == StateResolution(state=None, reason="no_matching_state")


def test_ambiguous_state_reports_all_matches() -> None:
    graph = _graph()
    result = resolve_issue_state(
        graph,
        _issue(labels=("symphony-ready-impl", "symphony-ready-review")),
    )

    assert result.state is None
    assert result.reason == "ambiguous_state"
    assert result.ambiguous_states == ("ready_impl", "ready_review")


def test_pr_delivered_transition_plans_review_handoff_not_done() -> None:
    graph = _graph()
    plan = plan_transition(
        graph,
        role_name="implementer",
        transition_name="pr_delivered",
        from_state_name="implementing",
        evidence=("pr_link",),
    )

    assert isinstance(plan, TransitionPlan)
    assert plan.from_state.name == "implementing"
    assert plan.to_state.name == "ready_review"
    assert plan.labels_to_remove == ("symphony-implementing",)
    assert plan.labels_to_add == ("symphony-ready-review",)
    assert plan.next_role == "reviewer"
    assert plan.next_actor == "human"
    assert plan.gate_owner is None


def test_design_needed_transition_plans_leader_gate() -> None:
    graph = _graph()
    plan = plan_transition(
        graph,
        role_name="implementer",
        transition_name="design_needed",
        from_state_name="implementing",
        evidence=("issue_comment",),
    )

    assert isinstance(plan, TransitionPlan)
    assert plan.to_state.name == "needs_design"
    assert plan.labels_to_add == ("symphony-needs-design",)
    assert plan.next_role == "leader"
    assert plan.next_actor == "hybrid"
    assert plan.gate_owner == "leader"


def test_claim_transition_moves_dispatchable_state_to_role_claim_state() -> None:
    graph = _graph()
    resolution = resolve_issue_state(graph, _issue(labels=("symphony-ready-impl",)))

    plan = plan_claim(graph, resolution)

    assert isinstance(plan, TransitionPlan)
    assert plan.transition.name == "claim:implementer"
    assert plan.from_state.name == "ready_impl"
    assert plan.to_state.name == "implementing"
    assert plan.labels_to_remove == ("symphony-ready-impl",)
    assert plan.labels_to_add == ("symphony-implementing",)
    assert plan.required_evidence == ("claim_comment",)


def test_claim_transition_requires_dispatch_role() -> None:
    graph = _graph()
    resolution = resolve_issue_state(graph, _issue(labels=("symphony-done",)))

    error = plan_claim(graph, resolution)

    assert isinstance(error, TransitionError)
    assert error.code == "no_dispatch_role"


def test_reverse_claim_returns_to_original_state() -> None:
    graph = _graph()
    resolution = resolve_issue_state(graph, _issue(labels=("symphony-ready-impl",)))
    claim = plan_claim(graph, resolution)
    assert isinstance(claim, TransitionPlan)

    reverse = plan_reverse_claim(graph, claim, reason="start-failed")

    assert reverse.transition.name == "release:implementer:start-failed"
    assert reverse.from_state.name == "implementing"
    assert reverse.to_state.name == "ready_impl"
    assert reverse.labels_to_remove == ("symphony-implementing",)
    assert reverse.labels_to_add == ("symphony-ready-impl",)


def test_missing_evidence_blocks_transition_plan() -> None:
    graph = _graph()
    error = plan_transition(
        graph,
        role_name="implementer",
        transition_name="pr_delivered",
        from_state_name="implementing",
        evidence=(),
    )

    assert isinstance(error, TransitionError)
    assert error.code == "missing_evidence"
    assert error.role == "implementer"
    assert error.transition == "pr_delivered"
    assert error.state == "implementing"


def test_invalid_transition_source_is_structured_error() -> None:
    graph = _graph()
    error = plan_transition(
        graph,
        role_name="implementer",
        transition_name="pr_delivered",
        from_state_name="ready_impl",
        evidence=("pr_link",),
    )

    assert isinstance(error, TransitionError)
    assert error.code == "source_state_not_allowed"


def test_role_cannot_run_another_roles_transition() -> None:
    graph = _graph()
    error = plan_transition(
        graph,
        role_name="reviewer",
        transition_name="pr_delivered",
        from_state_name="implementing",
        evidence=("pr_link",),
    )

    assert isinstance(error, TransitionError)
    assert error.code == "transition_role_mismatch"


def test_no_evidence_required_when_transition_requires_none() -> None:
    graph = _graph()
    plan = plan_transition(
        graph,
        role_name="reviewer",
        transition_name="approved",
        from_state_name="reviewing",
        evidence=(),
    )

    assert isinstance(plan, TransitionPlan)
    assert plan.required_evidence == ()
    assert plan.to_state.name == "approved"


def test_graph_for_config_returns_compatibility_graph_without_role_config() -> None:
    cfg = build_config(_minimal_raw(), workflow_path=Path("/tmp/W.md"), env={})
    graph = graph_for_config(cfg)

    assert graph.compatibility_mode is True
    assert graph.states["ready_impl"].labels == ("symphony-ready",)
    assert graph.states["implementing"].labels == ("symphony-running",)
    assert graph.states["blocked_operator"].labels == ("symphony-blocked",)
    assert graph.states["done"].labels == ("symphony-done",)

    result = resolve_issue_state(graph, _issue(labels=("symphony-ready",)))
    assert result.dispatchable is True
    assert result.dispatch_role is not None
    assert result.dispatch_role.name == "implementer"


def _graph() -> RoleGraphConfig:
    cfg = build_config(_minimal_raw() | _role_graph_raw(), workflow_path=Path("/tmp/W.md"), env={})
    assert cfg.role_graph is not None
    return cfg.role_graph


def _issue(labels: tuple[str, ...]) -> Issue:
    return Issue(
        id="I_1",
        number=1,
        identifier="acme/proj#1",
        owner="acme",
        repo="proj",
        title="Issue",
        body="Body",
        state="open",
        url="https://github.com/acme/proj/issues/1",
        labels=labels,
    )


def _minimal_raw() -> dict[str, object]:
    return {
        "tracker": {
            "kind": "github",
            "owner": "o",
            "repo": "r",
            "token": "literal-token",
        },
        "agent": {"provider": "claude_code"},
        "workspace": {"root": "ws"},
        "claude": {
            "model": "claude-opus-4-7",
            "permission_mode": "acceptEdits",
            "session_store": "s",
            "transcript_store": "t",
            "artifact_store": "a",
        },
        "github": {},
    }


def _role_graph_raw() -> dict[str, object]:
    return {
        "roles": {
            "implementer": {
                "actor": "agent",
                "provider": "claude_code",
                "can_claim": ["ready_impl", "changes_requested"],
                "claim_state": "implementing",
                "transitions": {
                    "pr_delivered": {
                        "from": "implementing",
                        "to": "ready_review",
                        "requires": "pr_link",
                    },
                    "design_needed": {
                        "from": "implementing",
                        "to": "needs_design",
                        "requires": "issue_comment",
                    },
                },
            },
            "reviewer": {
                "actor": "human",
                "can_claim": ["ready_review"],
                "claim_state": "reviewing",
                "transitions": {
                    "approved": {
                        "from": "reviewing",
                        "to": "approved",
                        "requires": "none",
                    },
                },
            },
            "leader": {
                "actor": "hybrid",
                "can_claim": ["needs_design"],
                "claim_state": "leader_reviewing",
            },
        },
        "states": {
            "ready_impl": {"labels": ["symphony-ready-impl"]},
            "implementing": {"labels": ["symphony-implementing"]},
            "ready_review": {"labels": ["symphony-ready-review"]},
            "reviewing": {"labels": ["symphony-reviewing"]},
            "changes_requested": {"labels": ["symphony-changes-requested"]},
            "needs_design": {
                "labels": ["symphony-needs-design"],
                "gate_owner": "leader",
            },
            "leader_reviewing": {"labels": ["symphony-leader-reviewing"]},
            "approved": {"labels": ["symphony-approved"]},
            "done": {"labels": ["symphony-done"], "terminal": True},
        },
    }
