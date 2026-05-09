from __future__ import annotations

from pathlib import Path

from symphony.evidence import DetectorResult
from tests.test_live_e2e_full import (
    _PERMISSION_MODE_ENV,
    _PR_DETECT_ATTEMPTS_ENV,
    _PR_DETECT_INTERVAL_ENV,
    _REQUIRE_PR_ENV,
    _build_claude_config,
    _detect_with_optional_pr_retry,
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


async def test_pr_required_detector_retries_until_pr_evidence(monkeypatch) -> None:
    monkeypatch.setenv(_PR_DETECT_ATTEMPTS_ENV, "3")
    monkeypatch.setenv(_PR_DETECT_INTERVAL_ENV, "0")

    class FakeDetector:
        def __init__(self) -> None:
            self.calls = 0

        def detect(self, **_kwargs):
            self.calls += 1
            if self.calls < 3:
                return DetectorResult(task_outcome="incomplete_no_evidence")
            return DetectorResult(
                task_outcome="completed_with_pr",
                task_evidence=[{"type": "pr_linked", "number": 188}],
            )

    detector = FakeDetector()
    result, retry = await _detect_with_optional_pr_retry(
        detector, {}, require_completed_with_pr=True
    )

    assert result.task_outcome == "completed_with_pr"
    assert detector.calls == 3
    assert retry == {
        "detector_attempts": 3,
        "detector_wait_seconds": 0.0,
        "detector_pr_retry_enabled": True,
    }


async def test_detector_does_not_retry_when_pr_not_required(monkeypatch) -> None:
    monkeypatch.setenv(_PR_DETECT_ATTEMPTS_ENV, "3")

    class FakeDetector:
        def __init__(self) -> None:
            self.calls = 0

        def detect(self, **_kwargs):
            self.calls += 1
            return DetectorResult(task_outcome="incomplete_no_evidence")

    detector = FakeDetector()
    result, retry = await _detect_with_optional_pr_retry(
        detector, {}, require_completed_with_pr=False
    )

    assert result.task_outcome == "incomplete_no_evidence"
    assert detector.calls == 1
    assert retry["detector_attempts"] == 1
