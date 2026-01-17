from pathlib import Path
from typing import Any

from litestar.contrib.jinja import JinjaTemplateEngine
from litestar.template import TemplateConfig


class Template:
    """WordPress-like template resolver with fallback support.

    Resolves templates in order of specificity:
    - Template("post", "about") → tries post-about.html, falls back to post.html
    - Template("page", "services", "web") → tries page-services-web.html → page-services.html → page.html
    """

    def __init__(self, template_type: str, *slugs: str, context: dict[str, Any] | None = None):
        self.template_type = template_type
        self.slugs = slugs
        self.context = context or {}
        self._resolved_template: str | None = None

    def resolve(self, template_dir: Path) -> str:
        """Resolve the most specific template that exists."""
        if self._resolved_template:
            return self._resolved_template

        # Build list of templates to try, from most to least specific
        templates_to_try = []

        if self.slugs:
            # Add progressively less specific templates
            for i in range(len(self.slugs), 0, -1):
                slug_part = "-".join(self.slugs[:i])
                templates_to_try.append(f"{self.template_type}-{slug_part}.html")

        # Always fall back to the base template type
        templates_to_try.append(f"{self.template_type}.html")

        # Find the first template that exists
        for template_name in templates_to_try:
            if (template_dir / template_name).exists():
                self._resolved_template = template_name
                return template_name

        # Default to base template even if it doesn't exist (let Jinja handle the error)
        self._resolved_template = f"{self.template_type}.html"
        return self._resolved_template

    def __repr__(self) -> str:
        return f"Template({self.template_type!r}, {', '.join(repr(s) for s in self.slugs)})"


def get_template_config(template_dir: Path) -> TemplateConfig:
    """Get the Jinja template configuration."""
    return TemplateConfig(
        directory=template_dir,
        engine=JinjaTemplateEngine,
    )
