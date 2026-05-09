from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_production_operations_runbook_covers_required_sections() -> None:
    doc = (ROOT / "docs" / "production-operations-runbook.md").read_text(
        encoding="utf-8"
    )

    for required in [
        "## Required Accounts And Secrets",
        "## Host Prerequisites",
        "## Preflight Checklist",
        "## systemd Service",
        "## launchd Service",
        "## Start, Stop, Restart",
        "## Recovery Procedures",
        "## Artifact And Cleanup Policy",
        "## Production Go/No-Go",
    ]:
        assert required in doc


def test_production_operations_runbook_preserves_key_safety_guidance() -> None:
    doc = (ROOT / "docs" / "production-operations-runbook.md").read_text(
        encoding="utf-8"
    )

    for required in [
        "Never put token literals in `WORKFLOW.md`",
        "make live-remote-claude",
        "make live-concurrency-e2e",
        "make live-lanes-e2e",
        "Do not delete workspaces or artifacts during a restart",
        "Verify no tracker token appears in remote worker payloads or logs",
        "No-go",
    ]:
        assert required in doc


def test_readme_and_readiness_docs_link_production_runbook() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    readiness = (ROOT / "docs" / "production-readiness.md").read_text(
        encoding="utf-8"
    )

    assert "docs/production-operations-runbook.md" in readme
    assert "docs/production-operations-runbook.md" in readiness
