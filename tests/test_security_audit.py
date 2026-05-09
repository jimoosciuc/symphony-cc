from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_makefile_exposes_security_audit_target() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")

    assert "security-audit:" in makefile
    for test_file in [
        "tests/test_security_profiles.py",
        "tests/test_dashboard.py",
        "tests/test_dashboard_server.py",
        "tests/test_remote_plan.py",
        "tests/test_remote_materialize.py",
        "tests/test_remote_ssh.py",
        "tests/test_remote_artifacts_ssh.py",
        "tests/test_orchestrator_remote.py",
        "tests/test_github_graphql_tool.py",
        "tests/test_artifact_retention.py",
    ]:
        assert test_file in makefile


def test_security_hardening_doc_covers_required_boundaries() -> None:
    doc = (ROOT / "docs" / "security-hardening.md").read_text(encoding="utf-8")

    for phrase in [
        "make security-audit",
        "Credential Boundaries",
        "GitHub Token Scopes",
        "Security Profiles",
        "Redaction Requirements",
        "Remote Worker Rules",
        "remote-worker-no-tracker-token",
        "Do not proceed",
    ]:
        assert phrase in doc


def test_operator_docs_link_security_audit() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    readiness = (ROOT / "docs" / "production-readiness.md").read_text(
        encoding="utf-8"
    )
    runbook = (ROOT / "docs" / "production-operations-runbook.md").read_text(
        encoding="utf-8"
    )

    assert "docs/security-hardening.md" in readme
    assert "make security-audit" in readiness
    assert "make security-audit" in runbook
