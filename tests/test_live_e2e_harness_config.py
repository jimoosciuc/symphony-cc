from __future__ import annotations

from pathlib import Path

from tests.test_live_e2e_full import (
    _PERMISSION_MODE_ENV,
    _REQUIRE_PR_ENV,
    _build_claude_config,
    _require_completed_with_pr,
)


def test_full_live_e2e_permission_mode_defaults_to_safe_mode(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv(_PERMISSION_MODE_ENV, raising=False)
    monkeypatch.delenv(_REQUIRE_PR_ENV, raising=False)

    config = _build_claude_config(tmp_path)

    assert config.permission_mode == "acceptEdits"
    assert _require_completed_with_pr() is False


def test_full_live_e2e_permission_mode_can_be_pr_capable(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv(_PERMISSION_MODE_ENV, "bypassPermissions")
    monkeypatch.setenv(_REQUIRE_PR_ENV, "1")

    config = _build_claude_config(tmp_path)

    assert config.permission_mode == "bypassPermissions"
    assert _require_completed_with_pr() is True


def test_docs_name_pr_required_live_e2e_environment() -> None:
    readme = Path("README.md").read_text(encoding="utf-8")
    readiness = Path("docs/production-readiness.md").read_text(encoding="utf-8")

    assert "SYMPHONY_E2E_PERMISSION_MODE=bypassPermissions" in readme
    assert "SYMPHONY_E2E_REQUIRE_PR=1" in readme
    assert "SYMPHONY_E2E_PERMISSION_MODE=bypassPermissions" in readiness
    assert "SYMPHONY_E2E_REQUIRE_PR=1" in readiness
