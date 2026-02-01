"""Tests for markdown rendering functionality."""

import pytest

from skrift.lib.markdown import render_markdown, get_renderer, create_markdown_renderer


class TestRenderMarkdownEmptyInput:
    """Tests for empty/None input handling."""

    def test_none_input_returns_empty_string(self):
        """None input returns empty string."""
        assert render_markdown(None) == ""

    def test_empty_string_returns_empty_string(self):
        """Empty string returns empty string."""
        assert render_markdown("") == ""

    def test_whitespace_only_returns_empty(self):
        """Whitespace-only input renders to empty output (markdown behavior)."""
        result = render_markdown("   ")
        assert result == ""


class TestRenderMarkdownBasicFormatting:
    """Tests for basic markdown formatting."""

    def test_paragraph(self):
        """Plain text becomes a paragraph."""
        result = render_markdown("Hello world")
        assert "<p>" in result
        assert "Hello world" in result

    def test_headings(self):
        """Headings are rendered correctly."""
        assert "<h1>" in render_markdown("# Heading 1")
        assert "<h2>" in render_markdown("## Heading 2")
        assert "<h3>" in render_markdown("### Heading 3")

    def test_bold(self):
        """Bold text is rendered."""
        result = render_markdown("**bold text**")
        assert "<strong>" in result
        assert "bold text" in result

    def test_italic(self):
        """Italic text is rendered."""
        result = render_markdown("*italic text*")
        assert "<em>" in result
        assert "italic text" in result

    def test_code_inline(self):
        """Inline code is rendered."""
        result = render_markdown("`code`")
        assert "<code>" in result
        assert "code" in result

    def test_code_block(self):
        """Code blocks are rendered."""
        result = render_markdown("```\ncode block\n```")
        assert "<pre>" in result
        assert "<code>" in result


class TestRenderMarkdownLinks:
    """Tests for link rendering."""

    def test_basic_link(self):
        """Basic link is rendered."""
        result = render_markdown("[link text](https://example.com)")
        assert '<a href="https://example.com"' in result
        assert "link text" in result

    def test_link_with_title(self):
        """Link with title attribute is rendered."""
        result = render_markdown('[link](https://example.com "My Title")')
        assert '<a href="https://example.com"' in result
        assert 'title="My Title"' in result


class TestRenderMarkdownImages:
    """Tests for image rendering."""

    def test_basic_image(self):
        """Basic image is rendered."""
        result = render_markdown("![Alt text](/images/photo.png)")
        assert "<img" in result
        assert 'src="/images/photo.png"' in result
        assert 'alt="Alt text"' in result

    def test_image_with_title(self):
        """Image with title is rendered."""
        result = render_markdown('![Alt](/images/photo.png "Image Title")')
        assert 'title="Image Title"' in result


class TestRenderMarkdownTables:
    """Tests for table rendering."""

    def test_basic_table(self):
        """Basic table is rendered."""
        markdown = """| Col 1 | Col 2 |
|-------|-------|
| A     | B     |
| C     | D     |"""
        result = render_markdown(markdown)
        assert "<table>" in result
        assert "<thead>" in result
        assert "<tbody>" in result
        assert "<tr>" in result
        assert "<th>" in result
        assert "<td>" in result

    def test_table_with_alignment(self):
        """Table with column alignment is rendered."""
        markdown = """| Left | Center | Right |
|:-----|:------:|------:|
| L    | C      | R     |"""
        result = render_markdown(markdown)
        assert "<table>" in result
        # Alignment should be in style attributes
        assert 'style="text-align:left"' in result
        assert 'style="text-align:center"' in result
        assert 'style="text-align:right"' in result


class TestRenderMarkdownFootnotes:
    """Tests for footnote rendering."""

    def test_single_footnote(self):
        """Single footnote is rendered."""
        markdown = """Text with footnote[^1].

[^1]: Footnote content."""
        result = render_markdown(markdown)
        # Check for footnote reference
        assert "footnote" in result.lower()

    def test_multiple_footnotes(self):
        """Multiple footnotes are rendered."""
        markdown = """First[^1] and second[^2].

[^1]: First footnote.
[^2]: Second footnote."""
        result = render_markdown(markdown)
        # Both footnotes should be present
        assert "First footnote" in result
        assert "Second footnote" in result


class TestRendererSingleton:
    """Tests for renderer singleton behavior."""

    def test_get_renderer_returns_same_instance(self):
        """get_renderer returns the same instance on multiple calls."""
        renderer1 = get_renderer()
        renderer2 = get_renderer()
        assert renderer1 is renderer2

    def test_create_markdown_renderer_returns_new_instance(self):
        """create_markdown_renderer creates a new instance each time."""
        renderer1 = create_markdown_renderer()
        renderer2 = create_markdown_renderer()
        assert renderer1 is not renderer2


class TestRenderMarkdownLists:
    """Tests for list rendering."""

    def test_unordered_list(self):
        """Unordered list is rendered."""
        markdown = """- Item 1
- Item 2
- Item 3"""
        result = render_markdown(markdown)
        assert "<ul>" in result
        assert "<li>" in result

    def test_ordered_list(self):
        """Ordered list is rendered."""
        markdown = """1. First
2. Second
3. Third"""
        result = render_markdown(markdown)
        assert "<ol>" in result
        assert "<li>" in result


class TestRenderMarkdownBlockquotes:
    """Tests for blockquote rendering."""

    def test_blockquote(self):
        """Blockquote is rendered."""
        result = render_markdown("> This is a quote")
        assert "<blockquote>" in result
        assert "This is a quote" in result
