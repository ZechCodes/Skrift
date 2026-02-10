"""Bound field class for template rendering."""

from __future__ import annotations

from typing import TYPE_CHECKING

from markupsafe import Markup, escape

if TYPE_CHECKING:
    from skrift.forms.core import Form


class BoundField:
    """A field bound to a form instance with current value, error state, and rendering helpers.

    Yielded by iterating a Form. Usable in templates as:
        {{ field }}              - full group (label + widget + error)
        {{ field.label_tag() }}  - just the label
        {{ field.widget() }}     - just the input/textarea/select
        {{ field.error }}        - error message or None
    """

    def __init__(self, form: Form, field_name: str):
        self.form = form
        self.name = field_name
        self._info = form.model.model_fields[field_name]
        extra = self._info.json_schema_extra
        self._extra: dict = extra if isinstance(extra, dict) else {}

    # -- Properties --

    @property
    def id(self) -> str:
        return f"field-{self.name}"

    @property
    def label(self) -> str:
        return self._extra.get("label", self.name.replace("_", " ").title())

    @property
    def value(self) -> str:
        return self.form.value(self.name)

    @property
    def error(self) -> str | None:
        return self.form.error(self.name)

    @property
    def required(self) -> bool:
        return self._info.is_required()

    @property
    def help_text(self) -> str | None:
        return self._extra.get("help_text")

    @property
    def widget_type(self) -> str:
        explicit = self._extra.get("widget")
        if explicit:
            return explicit
        return _infer_widget(self._info)

    @property
    def input_type(self) -> str:
        """HTML input type attribute for <input> elements."""
        explicit = self._extra.get("input_type")
        if explicit:
            return explicit

        type_map = {
            "EmailStr": "email",
            "SecretStr": "password",
        }
        annotation = self._info.annotation
        if annotation is not None and hasattr(annotation, "__name__"):
            return type_map.get(annotation.__name__, "text")
        return "text"

    @property
    def attrs(self) -> dict:
        """Extra HTML attributes from json_schema_extra['attrs']."""
        return self._extra.get("attrs", {})

    # -- Rendering --

    def label_tag(self) -> Markup:
        req = ' <span class="required">*</span>' if self.required else ""
        return Markup(f'<label for="{self.id}">{escape(self.label)}{req}</label>')

    def widget(self, **override_attrs) -> Markup:
        """Render the input/textarea/select element.

        Extra keyword arguments become HTML attributes:
            {{ field.widget(class_="wide", placeholder="...") }}
        """
        merged = {**self.attrs, **override_attrs}
        attrs_str = _render_attrs(merged)

        wt = self.widget_type

        if wt == "textarea":
            return Markup(
                f'<textarea id="{self.id}" name="{self.name}"{attrs_str}>'
                f"{escape(self.value)}</textarea>"
            )

        if wt == "select":
            choices = self._extra.get("choices", [])
            html = f'<select id="{self.id}" name="{self.name}"{attrs_str}>'
            for val, display in choices:
                selected = " selected" if str(val) == self.value else ""
                html += f'<option value="{escape(str(val))}"{selected}>{escape(str(display))}</option>'
            html += "</select>"
            return Markup(html)

        if wt == "checkbox":
            checked = " checked" if self.value else ""
            return Markup(
                f'<input type="checkbox" id="{self.id}" '
                f'name="{self.name}"{checked}{attrs_str}>'
            )

        # Default: <input type="...">
        return Markup(
            f'<input type="{self.input_type}" id="{self.id}" '
            f'name="{self.name}" value="{escape(self.value)}"{attrs_str}>'
        )

    def render(self) -> Markup:
        """Render label + widget + error as a complete field group."""
        html = str(self.label_tag()) + "\n" + str(self.widget())
        if self.error:
            html += f'\n<small class="error">{escape(self.error)}</small>'
        if self.help_text:
            html += f'\n<small class="text-muted">{escape(self.help_text)}</small>'
        return Markup(html)

    def __str__(self) -> str:
        return str(self.render())

    def __repr__(self) -> str:
        return f"BoundField({self.name!r}, value={self.value!r}, error={self.error!r})"


# -- Utilities --


def _infer_widget(field_info) -> str:
    """Infer widget type from Pydantic field annotation."""
    annotation = field_info.annotation
    if annotation is bool:
        return "checkbox"
    return "text"


def _render_attrs(attrs: dict) -> str:
    """Render a dict as HTML attributes string. Returns '' or ' key="val" key2="val2"'."""
    if not attrs:
        return ""
    parts = []
    for k, v in attrs.items():
        # Convert Python naming to HTML: class_ -> class, data_id -> data-id
        attr_name = k.rstrip("_").replace("_", "-")
        parts.append(f'{attr_name}="{escape(str(v))}"')
    return " " + " ".join(parts)
