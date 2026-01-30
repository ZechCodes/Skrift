"""Tests for middleware loading functionality."""

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from litestar.middleware import DefineMiddleware

from skrift.asgi import _load_middleware_factory, load_middleware


class TestLoadMiddlewareFactory:
    """Tests for _load_middleware_factory()."""

    def test_valid_spec_loads_correctly(self):
        """A valid spec like 'os.path:join' loads successfully."""
        factory = _load_middleware_factory("os.path:join")
        import os.path
        assert factory is os.path.join

    def test_missing_colon_raises_value_error(self):
        """Spec without colon raises ValueError."""
        with pytest.raises(ValueError, match="must be in format"):
            _load_middleware_factory("os.path.join")

    def test_multiple_colons_raises_value_error(self):
        """Spec with multiple colons raises ValueError."""
        with pytest.raises(ValueError, match="must contain exactly one colon"):
            _load_middleware_factory("os:path:join")

    def test_nonexistent_module_raises_import_error(self):
        """Nonexistent module raises ImportError."""
        with pytest.raises(ModuleNotFoundError):
            _load_middleware_factory("nonexistent_module_xyz:factory")

    def test_nonexistent_attribute_raises_attribute_error(self):
        """Nonexistent attribute raises AttributeError."""
        with pytest.raises(AttributeError):
            _load_middleware_factory("os:nonexistent_attribute_xyz")

    def test_non_callable_raises_type_error(self):
        """Non-callable attribute raises TypeError."""
        with pytest.raises(TypeError, match="is not callable"):
            _load_middleware_factory("os:name")


class TestLoadMiddleware:
    """Tests for load_middleware()."""

    def test_no_config_file_returns_empty_list(self, tmp_path):
        """When no config file exists, returns empty list."""
        nonexistent_path = tmp_path / "nonexistent.yaml"
        with patch("skrift.asgi.get_config_path", return_value=nonexistent_path):
            result = load_middleware()
        assert result == []

    def test_empty_config_returns_empty_list(self, temp_app_yaml):
        """When config file is empty, returns empty list."""
        config_path = temp_app_yaml({})
        # Actually write empty file
        with open(config_path, "w") as f:
            f.write("")

        with patch("skrift.asgi.get_config_path", return_value=config_path):
            result = load_middleware()
        assert result == []

    def test_no_middleware_section_returns_empty_list(self, temp_app_yaml):
        """When config has no middleware section, returns empty list."""
        config_path = temp_app_yaml({"controllers": []})

        with patch("skrift.asgi.get_config_path", return_value=config_path):
            result = load_middleware()
        assert result == []

    def test_empty_middleware_section_returns_empty_list(self, temp_app_yaml):
        """When middleware section is empty, returns empty list."""
        config_path = temp_app_yaml({"middleware": []})

        with patch("skrift.asgi.get_config_path", return_value=config_path):
            result = load_middleware()
        assert result == []

    def test_simple_string_spec_works(self, temp_app_yaml):
        """Simple string spec loads the factory."""
        config_path = temp_app_yaml({"middleware": ["os.path:join"]})

        with patch("skrift.asgi.get_config_path", return_value=config_path):
            result = load_middleware()

        import os.path
        assert len(result) == 1
        assert result[0] is os.path.join

    def test_dict_spec_without_kwargs_works(self, temp_app_yaml):
        """Dict spec without kwargs loads the factory."""
        config_path = temp_app_yaml({
            "middleware": [{"factory": "os.path:join"}]
        })

        with patch("skrift.asgi.get_config_path", return_value=config_path):
            result = load_middleware()

        import os.path
        assert len(result) == 1
        assert result[0] is os.path.join

    def test_dict_spec_with_kwargs_returns_define_middleware(self, temp_app_yaml, tmp_path):
        """Dict spec with kwargs returns DefineMiddleware instance."""
        # Create a test middleware module
        middleware_file = tmp_path / "test_middleware.py"
        middleware_file.write_text("""
def create_middleware(limit=10):
    def middleware(app):
        return app
    return middleware
""")

        # Add tmp_path to sys.path so we can import it
        sys.path.insert(0, str(tmp_path))

        try:
            config_path = temp_app_yaml({
                "middleware": [{
                    "factory": "test_middleware:create_middleware",
                    "kwargs": {"limit": 100}
                }]
            })

            with patch("skrift.asgi.get_config_path", return_value=config_path):
                result = load_middleware()

            assert len(result) == 1
            assert isinstance(result[0], DefineMiddleware)
        finally:
            sys.path.remove(str(tmp_path))
            # Clean up imported module
            if "test_middleware" in sys.modules:
                del sys.modules["test_middleware"]

    def test_missing_factory_key_raises_value_error(self, temp_app_yaml):
        """Dict spec without factory key raises ValueError."""
        config_path = temp_app_yaml({
            "middleware": [{"kwargs": {"limit": 100}}]
        })

        with patch("skrift.asgi.get_config_path", return_value=config_path):
            with pytest.raises(ValueError, match="must have 'factory' key"):
                load_middleware()

    def test_invalid_spec_type_raises_value_error(self, temp_app_yaml):
        """Invalid spec type raises ValueError."""
        config_path = temp_app_yaml({
            "middleware": [123]
        })

        with patch("skrift.asgi.get_config_path", return_value=config_path):
            with pytest.raises(ValueError, match="Invalid middleware spec type"):
                load_middleware()

    def test_multiple_middleware_specs_work(self, temp_app_yaml):
        """Multiple middleware specs are all loaded."""
        config_path = temp_app_yaml({
            "middleware": [
                "os.path:join",
                "os.path:dirname",
                {"factory": "os.path:basename"}
            ]
        })

        with patch("skrift.asgi.get_config_path", return_value=config_path):
            result = load_middleware()

        import os.path
        assert len(result) == 3
        assert result[0] is os.path.join
        assert result[1] is os.path.dirname
        assert result[2] is os.path.basename


class TestMiddlewareIntegration:
    """Integration tests for middleware loading."""

    def test_custom_middleware_factory_function_loads(self, tmp_path, temp_app_yaml):
        """Custom middleware factory function is imported correctly."""
        middleware_file = tmp_path / "custom_middleware.py"
        middleware_file.write_text("""
def logging_middleware_factory(app):
    async def middleware(scope, receive, send):
        await app(scope, receive, send)
    return middleware
""")

        sys.path.insert(0, str(tmp_path))

        try:
            config_path = temp_app_yaml({
                "middleware": ["custom_middleware:logging_middleware_factory"]
            })

            with patch("skrift.asgi.get_config_path", return_value=config_path):
                result = load_middleware()

            assert len(result) == 1
            assert callable(result[0])
            assert result[0].__name__ == "logging_middleware_factory"
        finally:
            sys.path.remove(str(tmp_path))
            if "custom_middleware" in sys.modules:
                del sys.modules["custom_middleware"]

    def test_custom_middleware_class_loads(self, tmp_path, temp_app_yaml):
        """Custom middleware class is imported correctly."""
        middleware_file = tmp_path / "class_middleware.py"
        middleware_file.write_text("""
class RateLimitMiddleware:
    def __init__(self, app, limit=10):
        self.app = app
        self.limit = limit

    async def __call__(self, scope, receive, send):
        await self.app(scope, receive, send)
""")

        sys.path.insert(0, str(tmp_path))

        try:
            config_path = temp_app_yaml({
                "middleware": ["class_middleware:RateLimitMiddleware"]
            })

            with patch("skrift.asgi.get_config_path", return_value=config_path):
                result = load_middleware()

            assert len(result) == 1
            assert result[0].__name__ == "RateLimitMiddleware"
        finally:
            sys.path.remove(str(tmp_path))
            if "class_middleware" in sys.modules:
                del sys.modules["class_middleware"]

    def test_cwd_added_to_sys_path(self, tmp_path, temp_app_yaml, monkeypatch):
        """Working directory is added to sys.path for local imports."""
        # Create middleware in tmp_path (which we'll pretend is cwd)
        middleware_file = tmp_path / "local_middleware.py"
        middleware_file.write_text("""
def local_factory(app):
    return app
""")

        # Change cwd to tmp_path
        monkeypatch.chdir(tmp_path)

        config_path = temp_app_yaml({
            "middleware": ["local_middleware:local_factory"]
        })

        with patch("skrift.asgi.get_config_path", return_value=config_path):
            result = load_middleware()

        assert len(result) == 1
        assert str(tmp_path) in sys.path

        # Clean up
        if "local_middleware" in sys.modules:
            del sys.modules["local_middleware"]
