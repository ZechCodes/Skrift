"""Tests for the code-declared content field system."""

from __future__ import annotations

import pytest

from skrift.content import (
    ContentArea,
    ContentModel,
    build_nodes,
    get_content_area,
    group,
    hydrate,
    list_content_areas,
    parse_nested_form,
    repeater,
    select,
    text,
    textarea,
)


class CTA(ContentModel):
    label: str = text("Button label", default="Go")
    url: str = text("Button link", default="/")


class Section(ContentModel):
    heading: str = text("Heading", default="")
    body: str = textarea("Body", default="")


class SampleHome(ContentArea, key="test-home", label="Test Home"):
    hero_title: str = text("Hero title", default="Welcome")
    plan: str = select(
        "Plan", choices=[("free", "Free"), ("pro", "Pro")], default="free"
    )
    cta: CTA = group(CTA, label="Call to action")
    sections: list[Section] = repeater(Section, label="Sections")


class TestRegistry:
    def test_area_is_registered_by_key(self):
        assert get_content_area("test-home") is SampleHome
        assert "test-home" in list_content_areas()

    def test_label_and_description_metadata(self):
        assert SampleHome._content_label == "Test Home"
        assert get_content_area("test-home")._content_key == "test-home"

    def test_unknown_key_raises(self):
        with pytest.raises(LookupError):
            get_content_area("does-not-exist")


class TestHydrate:
    def test_empty_data_fills_defaults(self):
        model = hydrate(SampleHome, {})
        assert model.hero_title == "Welcome"
        assert model.cta.label == "Go"
        assert model.sections == []

    def test_partial_data_merges_with_defaults(self):
        model = hydrate(SampleHome, {"hero_title": "Hi", "cta": {"label": "Join"}})
        assert model.hero_title == "Hi"
        assert model.cta.label == "Join"
        assert model.cta.url == "/"

    def test_unknown_keys_are_ignored(self):
        model = hydrate(SampleHome, {"removed_field": "x", "hero_title": "Kept"})
        assert model.hero_title == "Kept"
        assert not hasattr(model, "removed_field")

    def test_repeater_rows_validate(self):
        model = hydrate(
            SampleHome,
            {"sections": [{"heading": "A", "body": "b"}, {"heading": "C"}]},
        )
        assert [s.heading for s in model.sections] == ["A", "C"]
        assert model.sections[1].body == ""


class TestParseNestedForm:
    def test_flat_fields(self):
        assert parse_nested_form({"hero_title": "Hi"}) == {"hero_title": "Hi"}

    def test_skips_underscore_keys(self):
        assert parse_nested_form({"_csrf": "tok", "a": "1"}) == {"a": "1"}

    def test_nested_group(self):
        parsed = parse_nested_form({"cta.label": "Join", "cta.url": "/x"})
        assert parsed == {"cta": {"label": "Join", "url": "/x"}}

    def test_repeater_indices_become_list(self):
        parsed = parse_nested_form({
            "sections.0.heading": "A",
            "sections.0.body": "a",
            "sections.1.heading": "B",
            "sections.1.body": "b",
        })
        assert parsed == {
            "sections": [
                {"heading": "A", "body": "a"},
                {"heading": "B", "body": "b"},
            ]
        }

    def test_sparse_indices_are_compacted_in_order(self):
        # Rows 0 and 1 deleted in the admin; survivors keep their order.
        parsed = parse_nested_form({
            "sections.2.heading": "Third",
            "sections.5.heading": "Sixth",
        })
        assert parsed == {"sections": [{"heading": "Third"}, {"heading": "Sixth"}]}

    def test_roundtrip_through_schema(self):
        form = {
            "_csrf": "tok",
            "hero_title": "Launch",
            "plan": "pro",
            "cta.label": "Buy",
            "cta.url": "/buy",
            "sections.0.heading": "Why",
            "sections.0.body": "Because",
        }
        model = hydrate(SampleHome, parse_nested_form(form))
        assert model.hero_title == "Launch"
        assert model.plan == "pro"
        assert model.cta.label == "Buy"
        assert model.sections[0].heading == "Why"


class TestBuildNodes:
    def test_scalar_node_carries_widget_and_value(self):
        nodes = build_nodes(SampleHome, hydrate(SampleHome, {}).model_dump())
        title = next(n for n in nodes if n.get("name") == "hero_title")
        assert title["kind"] == "field"
        assert title["widget"] == "text"
        assert title["value"] == "Welcome"
        assert title["label"] == "Hero title"

    def test_select_node_includes_choices(self):
        nodes = build_nodes(SampleHome, hydrate(SampleHome, {}).model_dump())
        plan = next(n for n in nodes if n.get("name") == "plan")
        assert plan["widget"] == "select"
        assert ("pro", "Pro") in plan["choices"]

    def test_group_children_are_namespaced(self):
        nodes = build_nodes(SampleHome, hydrate(SampleHome, {}).model_dump())
        cta = next(n for n in nodes if n["kind"] == "group")
        child_names = [c["name"] for c in cta["children"]]
        assert "cta.label" in child_names
        assert "cta.url" in child_names

    def test_repeater_rows_and_template(self):
        data = hydrate(SampleHome, {"sections": [{"heading": "A"}]}).model_dump()
        nodes = build_nodes(SampleHome, data)
        rep = next(n for n in nodes if n["kind"] == "repeater")
        assert rep["name"] == "sections"
        # one populated row, namespaced by index
        assert rep["rows"][0][0]["name"] == "sections.0.heading"
        # blank template row carries the index placeholder for client cloning
        assert rep["template_nodes"][0]["name"] == "sections.__INDEX__.heading"
