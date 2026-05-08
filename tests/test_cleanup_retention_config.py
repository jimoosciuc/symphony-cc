"""Cleanup + retention config schema tests (#65 / M5.6).

Schema-only ticket per leader scope: no executor logic, no deletion.
These tests pin the contract that the M5.6 dataclasses + builders
produce, so the M5.6 #66 executor can rely on it.

Surface:
- defaults: omitted section → `enabled=False` everywhere; existing
  workflows keep loading unchanged
- valid policies: each trigger combo (terminal / closed-PR / age,
  with/without dry_run) round-trips through `build_config`
- invalid policies: enabled-with-no-trigger, negative max_age_days,
  wrong types, mistyped section keys all fail at workflow load with
  clear `ConfigError.location`
- workspace cleanup vs artifact retention modeled separately —
  retention is age-only (no terminal/PR-close triggers)
- path safety: cleanup config does NOT accept arbitrary paths
  (root path lives on `workspace.root` / `claude.artifact_store`,
  cleanup just toggles policy on those existing roots)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from symphony.config import (
    ArtifactRetentionConfig,
    ConfigError,
    WorkspaceCleanupConfig,
    build_config,
)

# -- Defaults: workflows without cleanup section keep working ----------------


def _base_raw(tmp_path: Path) -> dict[str, Any]:
    """Minimal valid workflow dict — used by every test to layer cleanup
    config on top of an otherwise standard workflow."""
    return {
        "tracker": {
            "kind": "github",
            "owner": "acme",
            "repo": "proj",
            "token": "literal-token",
        },
        "agent": {"provider": "claude_code"},
        "workspace": {"root": str(tmp_path / "ws")},
        "claude": {
            "model": "claude-opus-4-7",
            "permission_mode": "acceptEdits",
            "session_store": str(tmp_path / "sessions"),
            "transcript_store": str(tmp_path / "transcripts"),
            "artifact_store": str(tmp_path / "artifacts"),
        },
        "github": {},
    }


def _build(raw: dict[str, Any], tmp_path: Path) -> Any:
    return build_config(raw, workflow_path=tmp_path / "WORKFLOW.md", env={})


def test_workspace_cleanup_default_is_disabled(tmp_path: Path) -> None:
    """Default-safe: a workflow without `workspace.cleanup` MUST yield
    `cleanup.enabled=False`. SPEC §8 "preserved workspaces MUST be
    reused" semantics are unchanged for existing workflows."""
    cfg = _build(_base_raw(tmp_path), tmp_path)
    assert isinstance(cfg.workspace.cleanup, WorkspaceCleanupConfig)
    assert cfg.workspace.cleanup.enabled is False
    assert cfg.workspace.cleanup.on_terminal_issue is False
    assert cfg.workspace.cleanup.on_closed_pr is False
    assert cfg.workspace.cleanup.max_age_days is None
    assert cfg.workspace.cleanup.dry_run is False


def test_artifact_retention_default_is_disabled(tmp_path: Path) -> None:
    """Default-safe: artifacts are audit evidence and stay forever
    unless the operator opts in."""
    cfg = _build(_base_raw(tmp_path), tmp_path)
    assert isinstance(cfg.claude.artifact_retention, ArtifactRetentionConfig)
    assert cfg.claude.artifact_retention.enabled is False
    assert cfg.claude.artifact_retention.max_age_days is None
    assert cfg.claude.artifact_retention.dry_run is False


# -- Valid workspace.cleanup configs -----------------------------------------


def test_workspace_cleanup_terminal_issue_trigger(tmp_path: Path) -> None:
    raw = _base_raw(tmp_path)
    raw["workspace"]["cleanup"] = {"enabled": True, "on_terminal_issue": True}
    cfg = _build(raw, tmp_path)
    assert cfg.workspace.cleanup.enabled is True
    assert cfg.workspace.cleanup.on_terminal_issue is True
    assert cfg.workspace.cleanup.on_closed_pr is False
    assert cfg.workspace.cleanup.max_age_days is None


def test_workspace_cleanup_closed_pr_trigger(tmp_path: Path) -> None:
    raw = _base_raw(tmp_path)
    raw["workspace"]["cleanup"] = {"enabled": True, "on_closed_pr": True}
    cfg = _build(raw, tmp_path)
    assert cfg.workspace.cleanup.enabled is True
    assert cfg.workspace.cleanup.on_closed_pr is True


def test_workspace_cleanup_age_trigger(tmp_path: Path) -> None:
    raw = _base_raw(tmp_path)
    raw["workspace"]["cleanup"] = {"enabled": True, "max_age_days": 14}
    cfg = _build(raw, tmp_path)
    assert cfg.workspace.cleanup.enabled is True
    assert cfg.workspace.cleanup.max_age_days == 14


def test_workspace_cleanup_dry_run_round_trips(tmp_path: Path) -> None:
    raw = _base_raw(tmp_path)
    raw["workspace"]["cleanup"] = {
        "enabled": True,
        "on_terminal_issue": True,
        "dry_run": True,
    }
    cfg = _build(raw, tmp_path)
    assert cfg.workspace.cleanup.dry_run is True


def test_workspace_cleanup_combined_triggers(tmp_path: Path) -> None:
    """Operators can combine triggers — all three fire under the same
    `enabled=True` switch. Executor (#66) decides precedence."""
    raw = _base_raw(tmp_path)
    raw["workspace"]["cleanup"] = {
        "enabled": True,
        "on_terminal_issue": True,
        "on_closed_pr": True,
        "max_age_days": 30,
    }
    cfg = _build(raw, tmp_path)
    assert cfg.workspace.cleanup.on_terminal_issue is True
    assert cfg.workspace.cleanup.on_closed_pr is True
    assert cfg.workspace.cleanup.max_age_days == 30


def test_workspace_cleanup_disabled_with_triggers_set_is_inert(tmp_path: Path) -> None:
    """`enabled=False` with triggers set is a valid (if odd) shape — the
    executor (#66) MUST consult `enabled` first. Useful for operators
    who want to stage a config change before flipping the switch."""
    raw = _base_raw(tmp_path)
    raw["workspace"]["cleanup"] = {
        "enabled": False,
        "on_terminal_issue": True,
        "max_age_days": 14,
    }
    cfg = _build(raw, tmp_path)
    assert cfg.workspace.cleanup.enabled is False
    assert cfg.workspace.cleanup.on_terminal_issue is True
    assert cfg.workspace.cleanup.max_age_days == 14


# -- Invalid workspace.cleanup configs ---------------------------------------


def test_workspace_cleanup_enabled_without_trigger_rejected(tmp_path: Path) -> None:
    """Operator error: `enabled=True` with no trigger would never fire.
    Better to fail at workflow load than to ship a silently-broken
    cleanup config."""
    raw = _base_raw(tmp_path)
    raw["workspace"]["cleanup"] = {"enabled": True}
    with pytest.raises(ConfigError) as exc:
        _build(raw, tmp_path)
    assert exc.value.location == "workspace.cleanup"
    assert "trigger" in str(exc.value).lower()


def test_workspace_cleanup_negative_max_age_rejected(tmp_path: Path) -> None:
    raw = _base_raw(tmp_path)
    raw["workspace"]["cleanup"] = {"enabled": True, "max_age_days": -1}
    with pytest.raises(ConfigError) as exc:
        _build(raw, tmp_path)
    assert exc.value.location == "workspace.cleanup.max_age_days"


def test_workspace_cleanup_zero_max_age_treated_as_unset(tmp_path: Path) -> None:
    """Yaml `0` is operator's "off" — treat as unset. With no other
    trigger, this is the same as `enabled` without trigger → reject."""
    raw = _base_raw(tmp_path)
    raw["workspace"]["cleanup"] = {"enabled": True, "max_age_days": 0}
    with pytest.raises(ConfigError) as exc:
        _build(raw, tmp_path)
    assert "trigger" in str(exc.value).lower()


def test_workspace_cleanup_wrong_type_for_enabled_rejected(tmp_path: Path) -> None:
    raw = _base_raw(tmp_path)
    raw["workspace"]["cleanup"] = {"enabled": "yes"}  # str, not bool
    with pytest.raises(ConfigError) as exc:
        _build(raw, tmp_path)
    assert exc.value.location == "workspace.cleanup.enabled"


def test_workspace_cleanup_section_must_be_mapping(tmp_path: Path) -> None:
    raw = _base_raw(tmp_path)
    raw["workspace"]["cleanup"] = "not-a-mapping"
    with pytest.raises(ConfigError) as exc:
        _build(raw, tmp_path)
    assert exc.value.location == "workspace.cleanup"


# -- Valid claude.artifact_retention configs --------------------------------


def test_artifact_retention_age_trigger(tmp_path: Path) -> None:
    raw = _base_raw(tmp_path)
    raw["claude"]["artifact_retention"] = {"enabled": True, "max_age_days": 90}
    cfg = _build(raw, tmp_path)
    assert cfg.claude.artifact_retention.enabled is True
    assert cfg.claude.artifact_retention.max_age_days == 90


def test_artifact_retention_dry_run_round_trips(tmp_path: Path) -> None:
    raw = _base_raw(tmp_path)
    raw["claude"]["artifact_retention"] = {
        "enabled": True,
        "max_age_days": 30,
        "dry_run": True,
    }
    cfg = _build(raw, tmp_path)
    assert cfg.claude.artifact_retention.dry_run is True


# -- Invalid claude.artifact_retention configs ------------------------------


def test_artifact_retention_enabled_without_age_rejected(tmp_path: Path) -> None:
    """Per leader requirement on #65: artifact retention is age-only.
    Enabled-without-age would never delete — reject at load."""
    raw = _base_raw(tmp_path)
    raw["claude"]["artifact_retention"] = {"enabled": True}
    with pytest.raises(ConfigError) as exc:
        _build(raw, tmp_path)
    assert exc.value.location == "claude.artifact_retention"


def test_artifact_retention_negative_max_age_rejected(tmp_path: Path) -> None:
    raw = _base_raw(tmp_path)
    raw["claude"]["artifact_retention"] = {"enabled": True, "max_age_days": -7}
    with pytest.raises(ConfigError) as exc:
        _build(raw, tmp_path)
    assert exc.value.location == "claude.artifact_retention.max_age_days"


def test_artifact_retention_section_must_be_mapping(tmp_path: Path) -> None:
    raw = _base_raw(tmp_path)
    raw["claude"]["artifact_retention"] = ["not", "a", "mapping"]
    with pytest.raises(ConfigError) as exc:
        _build(raw, tmp_path)
    assert exc.value.location == "claude.artifact_retention"


# -- Schema separation: artifact_retention has narrower trigger surface -----


def test_artifact_retention_does_not_accept_terminal_issue_trigger(tmp_path: Path) -> None:
    """Per leader requirement: artifacts are audit evidence; only age
    matters. Operators cannot accidentally delete artifacts on terminal
    issue / closed PR — the schema doesn't expose those knobs.

    Locked in via the dataclass field set: extra keys in the YAML
    are silently ignored (consistent with the other `_opt_*` helpers),
    but a future operator who reads the dataclass surface will see
    only `enabled / max_age_days / dry_run`.
    """
    fields = {
        f for f in ArtifactRetentionConfig.__dataclass_fields__.keys()  # noqa: SIM118
    }
    assert fields == {"enabled", "max_age_days", "dry_run"}
    assert "on_terminal_issue" not in fields
    assert "on_closed_pr" not in fields


def test_workspace_cleanup_dataclass_has_full_trigger_surface() -> None:
    """Workspace cleanup gets the full trigger set per #65 spec."""
    fields = {
        f for f in WorkspaceCleanupConfig.__dataclass_fields__.keys()  # noqa: SIM118
    }
    assert fields == {
        "enabled",
        "on_terminal_issue",
        "on_closed_pr",
        "max_age_days",
        "dry_run",
    }


# -- Path normalization preserved (existing root unchanged) -----------------


def test_workspace_root_is_still_resolved_and_cleanup_does_not_accept_root(
    tmp_path: Path,
) -> None:
    """The cleanup config does NOT accept its own `root` field — the
    delete root is `workspace.root` (already resolved). This guards
    against a future operator who tries to pass a deletion target via
    the cleanup config (which #66 would then have to validate). Schema
    keeps the surface narrow."""
    raw = _base_raw(tmp_path)
    raw["workspace"]["cleanup"] = {
        "enabled": True,
        "on_terminal_issue": True,
        # Hypothetical operator typo / attempted scope-creep:
        "root": "/etc",
    }
    cfg = _build(raw, tmp_path)
    # The bogus `root` is ignored (not in dataclass), config loads cleanly,
    # and workspace.root is still derived from `workspace.root` only.
    assert "root" not in WorkspaceCleanupConfig.__dataclass_fields__
    assert cfg.workspace.root == (tmp_path / "ws").resolve()
