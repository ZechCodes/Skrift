"""Render the real content templates to catch macro and markup regressions."""

from __future__ import annotations

from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader

from skrift.content import build_nodes, hydrate
from skrift.content.builtin import HomeContent

TEMPLATES = Path(__file__).resolve().parent.parent / "skrift" / "templates"


@pytest.fixture
def env():
    return Environment(loader=FileSystemLoader([str(TEMPLATES)]), autoescape=True)


def test_templates_compile(env):
    # Parsing exercises all Jinja syntax in the templates.
    env.get_template("admin/content/edit.html")
    env.get_template("index.html")


def test_render_nodes_emits_namespaced_widgets(env):
    data = hydrate(HomeContent, {"sections": [{"heading": "Why us"}]}).model_dump()
    nodes = build_nodes(HomeContent, data)

    rendered = env.from_string(
        "{% from 'admin/content/_fields.html' import render_nodes %}{{ render_nodes(nodes) }}"
    ).render(nodes=nodes)

    # Scalar + group fields are namespaced with dotted paths.
    assert 'name="hero_title"' in rendered
    assert "<textarea" in rendered  # hero_subtitle
    assert 'name="cta.label"' in rendered
    assert 'name="cta.url"' in rendered

    # Existing repeater row is namespaced by index.
    assert 'name="sections.0.heading"' in rendered
    assert "Why us" in rendered

    # The blank repeater template carries the placeholder for client cloning.
    assert 'name="sections.__INDEX__.heading"' in rendered
    assert "data-repeater" in rendered
    assert "data-add" in rendered


def test_render_escapes_values(env):
    data = hydrate(HomeContent, {"hero_title": '<script>"x"'}).model_dump()
    nodes = build_nodes(HomeContent, data)
    rendered = env.from_string(
        "{% from 'admin/content/_fields.html' import render_nodes %}{{ render_nodes(nodes) }}"
    ).render(nodes=nodes)
    assert "<script>" not in rendered
    assert "&lt;script&gt;" in rendered
