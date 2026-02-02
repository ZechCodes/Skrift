# Sitemap & Robots.txt

Skrift automatically generates `sitemap.xml` and `robots.txt` for search engine optimization.

## Sitemap.xml

### Location

Your sitemap is available at:

```
https://yoursite.com/sitemap.xml
```

### What's Included

The sitemap automatically includes:

- All **published** pages
- Pages with past **publish_at** dates (scheduled pages that are now live)
- Last modified dates from `updated_at` or `created_at`

### What's Excluded

The sitemap automatically excludes:

- **Draft** pages (unpublished)
- **Scheduled** pages with future `publish_at` dates
- Pages excluded via the `sitemap_page` filter

### XML Format

```xml
<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url>
    <loc>https://yoursite.com/</loc>
    <lastmod>2026-01-15</lastmod>
    <changefreq>weekly</changefreq>
    <priority>1.0</priority>
  </url>
  <url>
    <loc>https://yoursite.com/about</loc>
    <lastmod>2026-01-10</lastmod>
    <changefreq>weekly</changefreq>
    <priority>0.8</priority>
  </url>
</urlset>
```

### Default Values

| Field | Value | Description |
|-------|-------|-------------|
| `changefreq` | `weekly` | How often content changes |
| `priority` | `1.0` for home, `0.8` for others | Relative importance |
| `lastmod` | `updated_at` or `created_at` | Last modification date |

## Robots.txt

### Location

Your robots.txt is available at:

```
https://yoursite.com/robots.txt
```

### Default Content

```
User-agent: *
Allow: /

Sitemap: https://yoursite.com/sitemap.xml
```

## Customization with Filters

### Exclude Pages from Sitemap

```python
from skrift.lib.hooks import filter

@filter("sitemap_page")
def exclude_internal_pages(entry, page):
    """Exclude pages with 'internal' in the slug."""
    if "internal" in page.slug:
        return None  # Returning None excludes the page
    return entry
```

### Modify Sitemap Entry

```python
from skrift.lib.hooks import filter

@filter("sitemap_page")
def adjust_priority(entry, page):
    """Give blog posts lower priority."""
    if page.slug.startswith("blog/"):
        entry.priority = 0.6
        entry.changefreq = "daily"
    return entry
```

### Add Custom URLs

```python
from skrift.lib.hooks import filter
from skrift.controllers.sitemap import SitemapEntry
from datetime import datetime, UTC

@filter("sitemap_urls")
def add_api_docs(entries):
    """Add non-page URLs to the sitemap."""
    entries.append(SitemapEntry(
        loc="https://yoursite.com/api/docs",
        lastmod=datetime(2026, 1, 1, tzinfo=UTC),
        changefreq="monthly",
        priority=0.5,
    ))
    return entries
```

### Customize robots.txt

```python
from skrift.lib.hooks import filter

@filter("robots_txt")
def customize_robots(content):
    """Add crawl delay and disallow admin."""
    return content + """
Crawl-delay: 10

User-agent: *
Disallow: /admin/
"""
```

## SitemapEntry Dataclass

When working with filters, entries use this structure:

```python
@dataclass
class SitemapEntry:
    loc: str                    # Full URL (required)
    lastmod: datetime | None    # Last modified date
    changefreq: str | None      # always, hourly, daily, weekly, monthly, yearly, never
    priority: float | None      # 0.0 to 1.0
```

## Enabling the Controller

The sitemap controller is included by default. If you've customized your `app.yaml`, ensure it's listed:

```yaml
controllers:
  - skrift.controllers.auth:AuthController
  - skrift.controllers.sitemap:SitemapController  # Add this line
  - skrift.admin.controller:AdminController
  - skrift.controllers.web:WebController
```

## Base URL Configuration

Sitemap URLs use the configured base URL:

1. **Site Settings** - `site_base_url` in admin settings
2. **Request URL** - Falls back to `request.base_url` if not set

For production, always set the base URL in settings to ensure consistent URLs.

## Search Engine Submission

After deployment, submit your sitemap to search engines:

- **Google**: [Search Console](https://search.google.com/search-console)
- **Bing**: [Webmaster Tools](https://www.bing.com/webmasters)

Or let search engines discover it via robots.txt (automatic).

## Caching

The sitemap and robots.txt are generated on each request. For high-traffic sites, consider:

1. Using a reverse proxy cache (nginx, Cloudflare)
2. Adding a filter that caches the result

```python
from skrift.lib.hooks import filter
from functools import lru_cache
from datetime import datetime, timedelta

_cache = {}
_cache_time = None

@filter("sitemap_urls")
def cache_sitemap(entries):
    """Simple 1-hour cache for sitemap."""
    global _cache, _cache_time

    now = datetime.now()
    if _cache_time and (now - _cache_time) < timedelta(hours=1):
        return _cache.get("entries", entries)

    _cache["entries"] = entries
    _cache_time = now
    return entries
```

## Troubleshooting

### Sitemap Returns 404

Ensure the sitemap controller is in your `app.yaml`:

```yaml
controllers:
  - skrift.controllers.sitemap:SitemapController
```

### Pages Missing from Sitemap

Check that pages are:

1. **Published** (`is_published = True`)
2. **Not scheduled for the future** (`publish_at` is None or in the past)
3. **Not excluded by a filter**

### Wrong Base URL

Set `site_base_url` in admin settings or ensure `OAUTH_REDIRECT_BASE_URL` environment variable is set.
