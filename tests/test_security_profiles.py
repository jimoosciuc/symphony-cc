"""Tests for M7.1 security profile validation (#100).

Covers security.profile config field, cross-field validation with
claude.permission_mode, and profile-specific warnings/errors.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from symphony.config import ConfigError, build_config


def _minimal_config(profile: str | None, permission_mode: str) -> dict:
    """Build minimal config dict with specified profile and permission_mode."""
    cfg = {
        "tracker": {
            "kind": "github",
            "owner": "test",
            "repo": "repo",
            "token": "literal-token",
        },
        "agent": {"provider": "claude_code"},
        "workspace": {"root": "/tmp/ws"},
        "claude": {
            "model": "test-model",
            "permission_mode": permission_mode,
            "session_store": "/tmp/sessions",
            "transcript_store": "/tmp/transcripts",
            "artifact_store": "/tmp/artifacts",
        },
        "github": {},
    }
    if profile is not None:
        cfg["security"] = {"profile": profile}
    return cfg


# -- Default profile ----------------------------------------------------------


def test_security_profile_defaults_to_conservative(tmp_path: Path) -> None:
    """When security section is missing, profile defaults to 'conservative'."""
    cfg_dict = _minimal_config(profile=None, permission_mode="acceptEdits")
    config = build_config(cfg_dict, workflow_path=tmp_path / "W.md")
    assert config.security.profile == "conservative"


def test_security_profile_explicit_conservative(tmp_path: Path) -> None:
    """Explicit 'conservative' profile loads without error."""
    cfg_dict = _minimal_config(profile="conservative", permission_mode="acceptEdits")
    config = build_config(cfg_dict, workflow_path=tmp_path / "W.md")
    assert config.security.profile == "conservative"
    assert len(config.warnings) == 0


# -- Profile validation -------------------------------------------------------


def test_security_profile_unknown_rejected(tmp_path: Path) -> None:
    """Unknown profile names are rejected with a clear error."""
    cfg_dict = _minimal_config(profile="unknown", permission_mode="acceptEdits")
    with pytest.raises(ConfigError) as excinfo:
        build_config(cfg_dict, workflow_path=tmp_path / "W.md")
    assert excinfo.value.location == "security.profile"
    assert "unknown" in str(excinfo.value)
    assert "conservative" in str(excinfo.value)


# -- restricted + bypassPermissions -------------------------------------------


def test_restricted_rejects_bypass_permissions(tmp_path: Path) -> None:
    """restricted profile + bypassPermissions is a config error."""
    cfg_dict = _minimal_config(profile="restricted", permission_mode="bypassPermissions")
    with pytest.raises(ConfigError) as excinfo:
        build_config(cfg_dict, workflow_path=tmp_path / "W.md")
    assert excinfo.value.location == "security.profile"
    assert "restricted" in str(excinfo.value)
    assert "bypassPermissions" in str(excinfo.value)


def test_restricted_accepts_accept_edits(tmp_path: Path) -> None:
    """restricted profile + acceptEdits loads successfully."""
    cfg_dict = _minimal_config(profile="restricted", permission_mode="acceptEdits")
    config = build_config(cfg_dict, workflow_path=tmp_path / "W.md")
    assert config.security.profile == "restricted"
    assert config.claude.permission_mode == "acceptEdits"
    # No high-risk warning for this combination.
    assert not any("trusted_unattended" in w.message for w in config.warnings)


# -- trusted_unattended + bypassPermissions -----------------------------------


def test_trusted_unattended_with_bypass_emits_warning(tmp_path: Path) -> None:
    """trusted_unattended + bypassPermissions loads but emits high-risk warning."""
    cfg_dict = _minimal_config(profile="trusted_unattended", permission_mode="bypassPermissions")
    config = build_config(cfg_dict, workflow_path=tmp_path / "W.md")
    assert config.security.profile == "trusted_unattended"
    assert config.claude.permission_mode == "bypassPermissions"
    # Should have both the bypassPermissions warning and the profile-specific warning.
    assert len(config.warnings) >= 2
    # Check for profile-specific warning.
    profile_warnings = [w for w in config.warnings if w.location == "security.profile"]
    assert len(profile_warnings) == 1
    assert "trusted_unattended" in profile_warnings[0].message
    assert "bypassPermissions" in profile_warnings[0].message


def test_trusted_unattended_with_accept_edits(tmp_path: Path) -> None:
    """trusted_unattended + acceptEdits loads without profile-specific warning."""
    cfg_dict = _minimal_config(profile="trusted_unattended", permission_mode="acceptEdits")
    config = build_config(cfg_dict, workflow_path=tmp_path / "W.md")
    assert config.security.profile == "trusted_unattended"
    assert config.claude.permission_mode == "acceptEdits"
    # No profile-specific warning for this combination.
    profile_warnings = [w for w in config.warnings if w.location == "security.profile"]
    assert len(profile_warnings) == 0


# -- conservative + bypassPermissions -----------------------------------------


def test_conservative_with_bypass_keeps_existing_warning(tmp_path: Path) -> None:
    """conservative + bypassPermissions keeps the existing bypassPermissions warning."""
    cfg_dict = _minimal_config(profile="conservative", permission_mode="bypassPermissions")
    config = build_config(cfg_dict, workflow_path=tmp_path / "W.md")
    assert config.security.profile == "conservative"
    assert config.claude.permission_mode == "bypassPermissions"
    # Should have the bypassPermissions warning but NOT a profile-specific warning.
    assert len(config.warnings) >= 1
    bypass_warnings = [w for w in config.warnings if "bypassPermissions" in w.message]
    assert len(bypass_warnings) >= 1
    # No profile-specific warning for conservative.
    profile_warnings = [w for w in config.warnings if w.location == "security.profile"]
    assert len(profile_warnings) == 0
