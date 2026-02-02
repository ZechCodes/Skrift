"""Sitemap and robots.txt controller for SEO."""

from dataclasses import dataclass
from datetime import datetime
from xml.etree.ElementTree import Element, SubElement, tostring

from litestar import Controller, Request, get
from litestar.response import Response
from sqlalchemy.ext.asyncio import AsyncSession

from skrift.db.services import page_service
from skrift.db.services.setting_service import get_cached_site_base_url
from skrift.lib.hooks import hooks, SITEMAP_PAGE, SITEMAP_URLS, ROBOTS_TXT


@dataclass
class SitemapEntry:
    """A single entry in the sitemap."""

    loc: str
    lastmod: datetime | None = None
    changefreq: str | None = None
    priority: float | None = None


class SitemapController(Controller):
    """Controller for sitemap.xml and robots.txt."""

    path = "/"

    @get("/sitemap.xml")
    async def sitemap(
        self, request: Request, db_session: AsyncSession
    ) -> Response:
        """Generate sitemap.xml with published pages."""
        base_url = get_cached_site_base_url() or str(request.base_url).rstrip("/")

        # Get all published pages (respects scheduling)
        pages = await page_service.list_pages(db_session, published_only=True)

        entries: list[SitemapEntry] = []

        for page in pages:
            slug = page.slug.strip("/")
            loc = f"{base_url}/{slug}" if slug else base_url

            entry = SitemapEntry(
                loc=loc,
                lastmod=page.updated_at or page.created_at,
                changefreq="weekly",
                priority=0.8 if slug else 1.0,  # Home page gets higher priority
            )

            # Apply sitemap_page filter (can return None to exclude)
            entry = await hooks.apply_filters(SITEMAP_PAGE, entry, page)
            if entry is not None:
                entries.append(entry)

        # Apply sitemap_urls filter to allow adding custom entries
        entries = await hooks.apply_filters(SITEMAP_URLS, entries)

        # Build XML
        xml = self._build_sitemap_xml(entries)

        return Response(
            content=xml,
            media_type="application/xml",
            headers={"Content-Type": "application/xml; charset=utf-8"},
        )

    def _build_sitemap_xml(self, entries: list[SitemapEntry]) -> bytes:
        """Build sitemap XML from entries."""
        urlset = Element("urlset")
        urlset.set("xmlns", "http://www.sitemaps.org/schemas/sitemap/0.9")

        for entry in entries:
            url = SubElement(urlset, "url")
            loc = SubElement(url, "loc")
            loc.text = entry.loc

            if entry.lastmod:
                lastmod = SubElement(url, "lastmod")
                lastmod.text = entry.lastmod.strftime("%Y-%m-%d")

            if entry.changefreq:
                changefreq = SubElement(url, "changefreq")
                changefreq.text = entry.changefreq

            if entry.priority is not None:
                priority = SubElement(url, "priority")
                priority.text = str(entry.priority)

        return b'<?xml version="1.0" encoding="UTF-8"?>\n' + tostring(urlset, encoding="utf-8")

    @get("/robots.txt")
    async def robots(
        self, request: Request, db_session: AsyncSession
    ) -> Response:
        """Generate robots.txt with sitemap reference."""
        base_url = get_cached_site_base_url() or str(request.base_url).rstrip("/")
        sitemap_url = f"{base_url}/sitemap.xml"

        content = f"""User-agent: *
Allow: /

Sitemap: {sitemap_url}
"""

        # Apply robots_txt filter for customization
        content = await hooks.apply_filters(ROBOTS_TXT, content)

        return Response(
            content=content,
            media_type="text/plain",
            headers={"Content-Type": "text/plain; charset=utf-8"},
        )
