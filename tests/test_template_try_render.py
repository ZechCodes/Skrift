from unittest.mock import patch

import jinja2
import pytest

from skrift.lib.template import Template


class MockTemplateEngine:
    def __init__(self, templates: dict[str, str]):
        self._env = jinja2.Environment(loader=jinja2.DictLoader(templates))

    def get_template(self, name: str):
        return self._env.get_template(name)


# --- _candidates() tests ---


def test_candidates_no_slugs():
    t = Template("form")
    assert t._candidates() == ["form.html"]


def test_candidates_one_slug():
    t = Template("form", "contact")
    assert t._candidates() == ["form-contact.html", "form.html"]


def test_candidates_two_slugs():
    t = Template("page", "services", "web")
    assert t._candidates() == [
        "page-services-web.html",
        "page-services.html",
        "page.html",
    ]


# --- try_render() tests ---


def test_try_render_returns_rendered_string_when_template_found():
    engine = MockTemplateEngine({"form.html": "Hello, {{ name }}!"})
    t = Template("form")
    result = t.try_render(engine, name="World")
    assert result == "Hello, World!"


def test_try_render_returns_none_when_no_template_exists():
    engine = MockTemplateEngine({})
    t = Template("form", "contact")
    result = t.try_render(engine)
    assert result is None


def test_try_render_uses_most_specific_template_first():
    engine = MockTemplateEngine(
        {
            "form-contact.html": "Contact form",
            "form.html": "Generic form",
        }
    )
    t = Template("form", "contact")
    result = t.try_render(engine)
    assert result == "Contact form"


def test_try_render_falls_back_to_less_specific_template():
    engine = MockTemplateEngine({"form.html": "Generic form"})
    t = Template("form", "contact")
    result = t.try_render(engine)
    assert result == "Generic form"


# --- resolve() directory-priority tests ---


def _create_template(directory, name):
    """Create a template file in the given directory."""
    path = directory / name
    path.write_text(f"template: {name}")


@patch("skrift.app_factory.get_template_directories_for_theme")
def test_resolve_theme_specific_wins_over_package_specific(mock_get_dirs, tmp_path):
    """Theme's specific template beats package's specific template."""
    theme_dir = tmp_path / "theme"
    package_dir = tmp_path / "package"
    theme_dir.mkdir()
    package_dir.mkdir()

    _create_template(theme_dir, "page-about.html")
    _create_template(package_dir, "page-about.html")

    mock_get_dirs.return_value = [theme_dir, package_dir]

    t = Template("page", "about")
    result = t.resolve(package_dir)
    assert result == "page-about.html"


@patch("skrift.app_factory.get_template_directories_for_theme")
def test_resolve_theme_generic_wins_over_package_specific(mock_get_dirs, tmp_path):
    """Theme's generic template beats package's more-specific template."""
    theme_dir = tmp_path / "theme"
    package_dir = tmp_path / "package"
    theme_dir.mkdir()
    package_dir.mkdir()

    _create_template(theme_dir, "page.html")
    _create_template(package_dir, "page-about.html")

    mock_get_dirs.return_value = [theme_dir, package_dir]

    t = Template("page", "about")
    result = t.resolve(package_dir)
    assert result == "page.html"


@patch("skrift.app_factory.get_template_directories_for_theme")
def test_resolve_package_specific_wins_when_no_theme(mock_get_dirs, tmp_path):
    """Package's specific template is used when no theme templates exist."""
    package_dir = tmp_path / "package"
    package_dir.mkdir()

    _create_template(package_dir, "page-about.html")

    mock_get_dirs.return_value = [package_dir]

    t = Template("page", "about")
    result = t.resolve(package_dir)
    assert result == "page-about.html"
