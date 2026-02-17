"""SEO utilities for generating page metadata."""

from dataclasses import dataclass
from html import escape
from typing import TYPE_CHECKING

from markupsafe import Markup

from skrift.lib.hooks import hooks, PAGE_SEO_META, PAGE_OG_META

if TYPE_CHECKING:
    from skrift.db.models import Page


def _meta_tag(name: str, content: str) -> str:
    return f'<meta name="{escape(name)}" content="{escape(content)}">'


def _og_tag(prop: str, content: str) -> str:
    return f'<meta property="{escape(prop)}" content="{escape(content)}">'


@dataclass
class SEOMeta:
    """Standard SEO metadata for a page."""

    title: str
    description: str | None
    canonical_url: str
    robots: str | None

    def __html__(self) -> str:
        parts: list[str] = []
        if self.description:
            parts.append(_meta_tag("description", self.description))
        if self.robots:
            parts.append(_meta_tag("robots", self.robots))
        if self.canonical_url:
            parts.append(f'<link rel="canonical" href="{escape(self.canonical_url)}">')
        return Markup("\n    ".join(parts))


@dataclass
class OpenGraphMeta:
    """OpenGraph metadata for social sharing."""

    title: str
    description: str | None
    image: str | None
    url: str
    site_name: str
    type: str = "website"

    def __html__(self) -> str:
        parts = [
            _og_tag("og:title", self.title),
            _og_tag("og:type", self.type),
            _og_tag("og:url", self.url),
            _og_tag("og:site_name", self.site_name),
        ]
        if self.description:
            parts.append(_og_tag("og:description", self.description))
        if self.image:
            parts.append(_og_tag("og:image", self.image))
        return Markup("\n    ".join(parts))


async def get_page_seo_meta(
    page: "Page",
    site_name: str,
    base_url: str,
) -> SEOMeta:
    """Generate SEO metadata for a page.

    Args:
        page: The page to generate metadata for
        site_name: The site name for title suffix
        base_url: Base URL for canonical URL generation

    Returns:
        SEOMeta dataclass with the metadata
    """
    # Build canonical URL
    slug = page.slug.strip("/")
    canonical_url = f"{base_url.rstrip('/')}/{slug}" if slug else base_url.rstrip("/")

    # Build title with site name suffix
    title = f"{page.title} | {site_name}" if site_name else page.title

    meta = SEOMeta(
        title=title,
        description=page.meta_description,
        canonical_url=canonical_url,
        robots=page.meta_robots,
    )

    # Apply filter for extensibility
    meta = await hooks.apply_filters(PAGE_SEO_META, meta, page, site_name, base_url)

    return meta


async def get_page_og_meta(
    page: "Page",
    site_name: str,
    base_url: str,
) -> OpenGraphMeta:
    """Generate OpenGraph metadata for a page.

    Args:
        page: The page to generate metadata for
        site_name: The site name
        base_url: Base URL for URL generation

    Returns:
        OpenGraphMeta dataclass with the metadata
    """
    # Build URL
    slug = page.slug.strip("/")
    url = f"{base_url.rstrip('/')}/{slug}" if slug else base_url.rstrip("/")

    # Use og_* fields if set, otherwise fall back to page fields
    og_title = page.og_title or page.title
    og_description = page.og_description or page.meta_description

    meta = OpenGraphMeta(
        title=og_title,
        description=og_description,
        image=page.og_image,
        url=url,
        site_name=site_name,
    )

    # Apply filter for extensibility
    meta = await hooks.apply_filters(PAGE_OG_META, meta, page, site_name, base_url)

    return meta
