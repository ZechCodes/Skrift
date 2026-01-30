"""Pytest fixtures for middleware loading tests."""

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml


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
