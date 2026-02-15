"""Markdown rendering utilities for page content."""

from markdown_it import MarkdownIt
from mdit_py_plugins.footnote import footnote_plugin


def create_markdown_renderer() -> MarkdownIt:
    """Create a configured markdown renderer with standard plugins."""
    md = MarkdownIt("commonmark", {"typographer": True, "html": False})
    md.enable("table")
    footnote_plugin(md)
    return md


_renderer: MarkdownIt | None = None


def get_renderer() -> MarkdownIt:
    """Get the singleton markdown renderer, creating it if needed."""
    global _renderer
    if _renderer is None:
        _renderer = create_markdown_renderer()
    return _renderer


def render_markdown(content: str) -> str:
    """Render markdown content to HTML.

    Args:
        content: Markdown text to render.

    Returns:
        Rendered HTML string. Returns empty string for empty/None input.
    """
    if not content:
        return ""
    return get_renderer().render(content)
