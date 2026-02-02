# SEO Metadata

Skrift provides built-in SEO support with meta tags, OpenGraph, and canonical URLs.

## Overview

Every page can have SEO metadata configured through the admin interface:

- **Meta Description** - Search engine snippet text
- **Meta Robots** - Indexing directives
- **OpenGraph Tags** - Social media sharing previews
- **Canonical URL** - Automatically generated

## Admin Configuration

Edit any page and expand the **SEO Settings** section:

| Field | Description | Best Practice |
|-------|-------------|---------------|
| **Meta Description** | 50-160 character summary | Unique per page, include keywords |
| **Meta Robots** | Indexing directive | Leave default for most pages |
| **OG Title** | Social share title | Use if different from page title |
| **OG Description** | Social share description | Use if different from meta description |
| **OG Image URL** | Social share image | 1200x630px recommended |

## Generated HTML

Skrift automatically renders SEO tags in the `<head>`:

```html
<!-- SEO Meta -->
<meta name="description" content="Your meta description">
<meta name="robots" content="index, follow">
<link rel="canonical" href="https://yoursite.com/page-slug">

<!-- OpenGraph -->
<meta property="og:title" content="Page Title">
<meta property="og:description" content="Page description">
<meta property="og:image" content="https://yoursite.com/image.jpg">
<meta property="og:url" content="https://yoursite.com/page-slug">
<meta property="og:site_name" content="Your Site Name">
<meta property="og:type" content="website">
```

## Template Blocks

The base template provides blocks for customization:

```html
{% block seo_meta %}
  {# Override entire SEO meta section #}
{% endblock %}

{% block og_meta %}
  {# Override entire OpenGraph section #}
{% endblock %}
```

## Programmatic Access

### In Controllers

```python
from skrift.lib.seo import get_page_seo_meta, get_page_og_meta
from skrift.db.services.setting_service import get_cached_site_name, get_cached_site_base_url

async def my_handler(request, db_session, page):
    site_name = get_cached_site_name()
    base_url = get_cached_site_base_url() or str(request.base_url)

    seo_meta = await get_page_seo_meta(page, site_name, base_url)
    og_meta = await get_page_og_meta(page, site_name, base_url)

    return TemplateResponse("page.html", context={
        "seo_meta": seo_meta,
        "og_meta": og_meta,
    })
```

### SEOMeta Dataclass

```python
@dataclass
class SEOMeta:
    title: str           # "Page Title | Site Name"
    description: str     # Meta description or None
    canonical_url: str   # Full canonical URL
    robots: str          # Meta robots or None
```

### OpenGraphMeta Dataclass

```python
@dataclass
class OpenGraphMeta:
    title: str           # OG title (falls back to page title)
    description: str     # OG description or None
    image: str           # OG image URL or None
    url: str             # Full page URL
    site_name: str       # Site name
    type: str            # "website" (default)
```

## Extending with Filters

Use hooks to modify SEO metadata globally:

### Add Author Meta

```python
from skrift.lib.hooks import filter

@filter("page_seo_meta")
async def add_author(meta, page, site_name, base_url):
    # Access page.user if available
    if page.user:
        # Meta object is a dataclass, modify as needed
        pass
    return meta
```

### Add Twitter Cards

```python
from skrift.lib.hooks import filter

@filter("page_og_meta")
async def enhance_social(meta, page, site_name, base_url):
    # Add Twitter-specific data to meta or handle in template
    return meta
```

## Base URL Configuration

The base URL for canonical and OG URLs comes from:

1. **Site Settings** - `site_base_url` setting in admin
2. **Request URL** - Falls back to `request.base_url` if not set

Set the base URL in production:

```bash
# Via admin: Settings > Site Base URL
# Or in database settings table
```

## Meta Robots Options

Available options in the admin dropdown:

| Value | Effect |
|-------|--------|
| *Default* | index, follow (search engines index and follow links) |
| `noindex` | Don't index this page in search results |
| `nofollow` | Don't follow links on this page |
| `noindex, nofollow` | Don't index or follow |

## Best Practices

### Meta Descriptions

- Write unique descriptions for each page
- Keep under 160 characters (155 ideal)
- Include relevant keywords naturally
- Make it compelling - it's your search result snippet

### OpenGraph Images

- Use 1200x630 pixels for best display
- Ensure important content is centered
- Use absolute URLs (https://...)
- Test with Facebook's [Sharing Debugger](https://developers.facebook.com/tools/debug/)

### Canonical URLs

- Automatically generated from page slug
- Helps prevent duplicate content issues
- Always uses the configured base URL

### Indexing Decisions

- Use `noindex` for:
  - Thank you / confirmation pages
  - Internal-only content
  - Duplicate or thin content

- Use `nofollow` sparingly:
  - User-generated content links
  - Paid/sponsored links

## Sitemap Integration

Pages with SEO metadata are automatically included in `/sitemap.xml`:

- Only published pages are included
- Scheduled pages appear after their publish date
- Use the `sitemap_page` filter to customize or exclude pages
- Last modified date is automatically set

See [Sitemap & Robots.txt](../reference/sitemap-robots.md) for more details.
