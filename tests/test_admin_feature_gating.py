"""Admin sub-controllers tied to a config-gated feature only register when the
feature is enabled, so disabled features 404 (and drop out of the admin nav)."""

from __future__ import annotations

import pytest


def _load_admin_controllers(mock_config_path, *, workers, webhooks, oauth2, api_keys):
    """Run load_controllers() for a config with the AdminController and the
    feature flags set in app.yaml."""
    config = {
        "controllers": ["skrift.admin.controller:AdminController"],
        "oauth2_enabled": oauth2,
        "api_keys": {"enabled": api_keys},
        "workers": {"enabled": workers},
        "webhooks": {"enabled": webhooks},
    }
    _patched, patcher = mock_config_path(config)
    try:
        from skrift.asgi import load_controllers

        controllers = load_controllers()
    finally:
        patcher.stop()
    return {c.__name__ for c in controllers}


CORE = {
    "UserAdminController",
    "SettingsAdminController",
    "ContentAdminController",
    "MediaAdminController",
}

GATED = {
    "WorkersAdminController",
    "AgentUsageAdminController",
    "WebhooksAdminController",
    "OAuth2ClientAdminController",
    "APIKeyAdminController",
}


class TestAdminFeatureGating:
    def test_disabled_features_are_not_registered(self, mock_config_path):
        names = _load_admin_controllers(
            mock_config_path, workers=False, webhooks=False, oauth2=False, api_keys=False
        )
        assert not (GATED & names), f"gated controllers leaked: {GATED & names}"
        assert CORE <= names, f"core controllers missing: {CORE - names}"

    def test_enabled_features_are_registered(self, mock_config_path):
        names = _load_admin_controllers(
            mock_config_path, workers=True, webhooks=True, oauth2=True, api_keys=True
        )
        assert GATED <= names, f"enabled controllers missing: {GATED - names}"
        assert CORE <= names

    def test_workers_gate_covers_agent_usage(self, mock_config_path):
        # Agent usage calls get_runtime(), so it is gated on the worker runtime.
        names = _load_admin_controllers(
            mock_config_path, workers=False, webhooks=True, oauth2=True, api_keys=True
        )
        assert "WorkersAdminController" not in names
        assert "AgentUsageAdminController" not in names
        assert "WebhooksAdminController" in names

    @pytest.mark.parametrize("flag", ["workers", "webhooks", "oauth2", "api_keys"])
    def test_each_flag_gates_independently(self, mock_config_path, flag):
        kwargs = {"workers": True, "webhooks": True, "oauth2": True, "api_keys": True}
        kwargs[flag] = False
        names = _load_admin_controllers(mock_config_path, **kwargs)
        expected_absent = {
            "workers": {"WorkersAdminController", "AgentUsageAdminController"},
            "webhooks": {"WebhooksAdminController"},
            "oauth2": {"OAuth2ClientAdminController"},
            "api_keys": {"APIKeyAdminController"},
        }[flag]
        assert not (expected_absent & names), f"{flag} off but present: {expected_absent & names}"
