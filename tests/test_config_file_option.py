"""Tests for the -f / --config-file CLI option and config path override."""

import os
from unittest.mock import patch

import yaml
from click.testing import CliRunner

import skrift.config as config_mod
from skrift.cli import cli
from skrift.config import get_config_path, set_config_path, clear_settings_cache


class TestSetConfigPath:
    """Test set_config_path / get_config_path override behaviour."""

    def setup_method(self):
        config_mod._config_path_override = None

    def teardown_method(self):
        config_mod._config_path_override = None

    def test_override_is_returned(self, tmp_path):
        custom = tmp_path / "custom.yaml"
        set_config_path(custom)
        assert get_config_path() == custom

    def test_falls_back_to_env_based_resolution(self):
        """Without an override, get_config_path uses SKRIFT_ENV logic."""
        with patch.dict(os.environ, {"SKRIFT_ENV": "testing"}, clear=False):
            path = get_config_path()
        assert path.name == "app.testing.yaml"

    def test_production_fallback(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SKRIFT_ENV", None)
            path = get_config_path()
        assert path.name == "app.yaml"


class TestEnvironmentKeyInYaml:
    """Test that an 'environment' key in the config sets SKRIFT_ENV."""

    def setup_method(self):
        config_mod._config_path_override = None
        clear_settings_cache()

    def teardown_method(self):
        config_mod._config_path_override = None
        clear_settings_cache()

    def test_environment_key_sets_env_var(self, tmp_path):
        config_file = tmp_path / "app.yaml"
        config_file.write_text(
            yaml.safe_dump({"environment": "staging", "debug": True})
        )
        set_config_path(config_file)

        old_env = os.environ.get("SKRIFT_ENV")
        try:
            with patch.dict(os.environ, {"SECRET_KEY": "test-secret"}, clear=False):
                from skrift.config import get_settings

                get_settings()
                assert os.environ["SKRIFT_ENV"] == "staging"
        finally:
            if old_env is None:
                os.environ.pop("SKRIFT_ENV", None)
            else:
                os.environ["SKRIFT_ENV"] = old_env


class TestCliConfigFileOption:
    """Test CLI -f / --config-file integration."""

    def setup_method(self):
        config_mod._config_path_override = None

    def teardown_method(self):
        config_mod._config_path_override = None

    def test_config_file_option_sets_override(self, tmp_path):
        config_file = tmp_path / "my-config.yaml"
        config_file.write_text(yaml.safe_dump({"debug": True}))

        runner = CliRunner()
        # Use `secret` subcommand — it just prints a key and exits
        result = runner.invoke(cli, ["-f", str(config_file), "secret"])
        assert result.exit_code == 0
        assert config_mod._config_path_override == config_file

    def test_missing_config_file_errors(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["-f", "/nonexistent/config.yaml", "secret"])
        assert result.exit_code != 0

    def test_no_flag_leaves_override_unset(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["--help"])
        assert result.exit_code == 0
        assert config_mod._config_path_override is None
