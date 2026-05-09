from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_makefile_exposes_failure_drill_target() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")

    assert "failure-drills:" in makefile
    for test_file in [
        "tests/test_recovery.py",
        "tests/test_github_tracker.py",
        "tests/test_evidence.py",
        "tests/test_routing.py",
        "tests/test_timeouts.py",
        "tests/test_remote_ssh.py",
        "tests/test_remote_runner.py",
        "tests/test_orchestrator_remote.py",
        "tests/test_cleanup_executor.py",
        "tests/test_workflow_reload.py",
    ]:
        assert test_file in makefile


def test_failure_drill_doc_covers_required_failure_modes() -> None:
    doc = (ROOT / "docs" / "failure-injection-drills.md").read_text(
        encoding="utf-8"
    )

    for phrase in [
        "Daemon restart",
        "GitHub 429",
        "Claude 503",
        "Remote SSH",
        "Workspace cleanup",
        "Invalid workflow reload",
        "make live-remote-claude",
        "make live-concurrency-e2e",
        "no provider or infrastructure error is reported as completed work",
    ]:
        assert phrase in doc


def test_operator_docs_link_failure_drills() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    readiness = (ROOT / "docs" / "production-readiness.md").read_text(
        encoding="utf-8"
    )
    runbook = (ROOT / "docs" / "production-operations-runbook.md").read_text(
        encoding="utf-8"
    )

    assert "docs/failure-injection-drills.md" in readme
    assert "make failure-drills" in readiness
    assert "make failure-drills" in runbook
