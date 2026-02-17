from pathlib import Path
from typing import Any

import jinja2
from litestar.contrib.jinja import JinjaTemplateEngine
from litestar.response import Template as TemplateResponse
from litestar.template import TemplateConfig


class Template:
    """WordPress-like template resolver with fallback support.

    Resolves templates in order of specificity:
    - Template("post", "about") → tries post-about.html, falls back to post.html
    - Template("page", "services", "web") → tries page-services-web.html → page-services.html → page.html

    Template Directory Hierarchy:
    Templates are searched in the following order:
    1. themes/<active>/templates/ (active theme) - Theme overrides
    2. ./templates/ (working directory) - User overrides
    3. skrift/templates/ (package directory) - Default templates

    Available Templates for Override:
    - base.html - Base layout template
    - index.html - Homepage template
    - page.html - Default page template
    - post.html - Default post template
    - error.html - Generic error page
    - error-404.html - Not found error page
    - error-500.html - Server error page

    Users can override any template by creating a file with the same name
    in their project's ./templates/ directory, or by using a theme.
    """

    def __init__(self, template_type: str, *slugs: str, context: dict[str, Any] | None = None):
        self.template_type = template_type
        self.slugs = slugs
        self.context = context or {}
        self._resolved_template: str | None = None

    def _candidates(self) -> list[str]:
        """Build list of template names to try, from most to least specific."""
        candidates = []
        if self.slugs:
            for i in range(len(self.slugs), 0, -1):
                slug_part = "-".join(self.slugs[:i])
                candidates.append(f"{self.template_type}-{slug_part}.html")
        candidates.append(f"{self.template_type}.html")
        return candidates

    def resolve(self, template_dir: Path, theme_name: str = "") -> str:
        """Resolve the most specific template that exists.

        Searches for templates in order:
        1. themes/<theme_name>/templates/ (if theme_name is set)
        2. Working directory's ./templates/
        3. Package's templates directory

        Within each directory, searches from most to least specific template name.
        """
        if self._resolved_template:
            return self._resolved_template

        from skrift.app_factory import get_template_directories_for_theme

        search_dirs = get_template_directories_for_theme(theme_name)

        # Search for templates in each directory
        for template_name in self._candidates():
            for search_dir in search_dirs:
                template_path = search_dir / template_name
                if template_path.exists():
                    self._resolved_template = template_name
                    return template_name

        # Default to base template even if it doesn't exist (let Jinja handle the error)
        self._resolved_template = f"{self.template_type}.html"
        return self._resolved_template

    def try_render(self, template_engine, **context) -> str | None:
        """Attempt to render using the template hierarchy.

        Iterates candidates from most to least specific, using the template
        engine to render. Returns the rendered string, or None if no matching
        template exists.
        """
        for candidate in self._candidates():
            try:
                template = template_engine.get_template(candidate)
                return template.render(**context)
            except jinja2.TemplateNotFound:
                continue
        return None

    def render(self, template_dir: Path, theme_name: str = "", **extra_context: Any) -> TemplateResponse:
        """Resolve template and return TemplateResponse with merged context.

        Context passed to __init__ is merged with extra_context, with extra_context
        taking precedence for duplicate keys.

        Args:
            template_dir: Package template directory (used as fallback).
            theme_name: Active theme name for directory resolution.
            **extra_context: Additional context merged with init context.
        """
        template_name = self.resolve(template_dir, theme_name=theme_name)
        merged_context = {**self.context, **extra_context}
        return TemplateResponse(template_name, context=merged_context)

    def __repr__(self) -> str:
        return f"Template({self.template_type!r}, {', '.join(repr(s) for s in self.slugs)})"


def get_template_config(template_dir: Path) -> TemplateConfig:
    """Get the Jinja template configuration.

    Configures Jinja to search for templates in multiple directories:
    1. themes/<active>/templates/ (if a theme is active)
    2. ./templates/ (working directory) - for user overrides
    3. package templates directory - for default templates
    """
    from skrift.app_factory import get_template_directories

    return TemplateConfig(
        directory=get_template_directories(),
        engine=JinjaTemplateEngine,
    )
