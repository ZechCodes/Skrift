"""Shared pytest fixtures."""

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from skrift.lib.hooks import hooks


@pytest.fixture
def temp_app_yaml(tmp_path):
    """Create a temporary app.yaml file for testing."""
    config_path = tmp_path / "app.yaml"

    def _create_config(config: dict):
        with open(config_path, "w") as f:
            yaml.safe_dump(config, f)
        return config_path

    return _create_config


@pytest.fixture
def mock_config_path(temp_app_yaml):
    """Fixture that patches get_config_path to return a custom path."""
    def _mock(config: dict):
        config_path = temp_app_yaml(config)
        patcher = patch("skrift.asgi.get_config_path", return_value=config_path)
        return patcher.start(), patcher

    return _mock


@pytest.fixture(autouse=True)
def clean_sys_path():
    """Ensure sys.path is restored after each test."""
    original_path = sys.path.copy()
    yield
    sys.path = original_path


@pytest.fixture
def clean_hooks():
    """Save and restore hooks state around a test."""
    original_filters = hooks._filters.copy()
    original_actions = hooks._actions.copy()
    yield
    hooks._filters = original_filters
    hooks._actions = original_actions


@pytest.fixture
def mock_request_factory():
    """Factory fixture that returns mock requests with a session dict."""
    def _make(session=None, form_data=None):
        request = MagicMock()
        request.session = session if session is not None else {}
        if form_data is not None:
            async def _form():
                return form_data
            request.form = _form
        return request
    return _make
