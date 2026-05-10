from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_makefile_exposes_full_live_validation_matrix() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")

    required_targets = [
        "live-github:",
        "live-graphql:",
        "live-claude:",
        "live-remote:",
        "live-e2e:",
        "live-role-github-e2e:",
        "live-remote-claude:",
        "live-concurrency-e2e:",
        "live-lanes-e2e:",
        "live-validation:",
    ]
    for target in required_targets:
        assert target in makefile

    live_validation_line = next(
        line for line in makefile.splitlines() if line.startswith("live-validation:")
    )
    for dependency in [
        "live-github",
        "live-graphql",
        "live-claude",
        "live-remote",
        "live-e2e",
        "live-role-github-e2e",
        "live-remote-claude",
        "live-concurrency-e2e",
        "live-lanes-e2e",
    ]:
        assert dependency in live_validation_line


def test_manual_live_workflow_exposes_all_validation_targets() -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "live-integration.yml").read_text(
            encoding="utf-8"
        )
    )
    target_input = workflow[True]["workflow_dispatch"]["inputs"]["target"]

    assert set(target_input["options"]) >= {
        "all",
        "github",
        "graphql",
        "claude",
        "remote",
        "full-e2e",
        "role-github-e2e",
        "remote-claude",
        "concurrency-e2e",
        "lanes-e2e",
        "validation-matrix",
    }

    step_runs = [
        step.get("run", "")
        for step in workflow["jobs"]["live-integration"]["steps"]
        if isinstance(step, dict)
    ]
    for command in [
        "make live-github",
        "make live-graphql",
        "make live-claude",
        "make live-remote",
        "make live-e2e",
        "make live-role-github-e2e",
        "make live-remote-claude",
        "make live-concurrency-e2e",
        "make live-lanes-e2e",
    ]:
        assert command in step_runs


def test_production_readiness_doc_distinguishes_ci_from_production_ready() -> None:
    doc = (ROOT / "docs" / "production-readiness.md").read_text(encoding="utf-8")

    assert "Passing default CI is necessary, but it is not enough" in doc
    assert "make live-validation" in doc
    assert "Do not claim production readiness before L4" in doc
    assert "SYMPHONY_RUN_REMOTE_CLAUDE_E2E=1" in doc
    assert "SYMPHONY_RUN_ROLE_GITHUB_E2E=1" in doc
    assert "SYMPHONY_RUN_CONCURRENCY_E2E=1" in doc
    assert "SYMPHONY_RUN_LANES_E2E=1" in doc
