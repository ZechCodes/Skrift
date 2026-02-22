# Creating Pages

<span class="skill-badge beginner">:material-star: Beginner</span>

Learn how to create and manage pages in your Skrift site.

## Overview

Pages are stored in the database and accessed by their URL slug. A page with slug `about` is available at `/about`.

## Creating a Page

```python
from skrift.db.services import page_service

page = await page_service.create_page(
    db_session,
    slug="about",
    title="About Us",
    content="<p>Welcome to our site!</p>",
    is_published=True,
)
```

### Page Fields

| Field | Required | Description |
|-------|----------|-------------|
| `slug` | Yes | URL path identifier (e.g., "about", "contact") |
| `title` | Yes | Page title |
| `content` | No | HTML content |
| `is_published` | No | Whether the page is visible to anonymous users |
| `published_at` | No | Publication timestamp |

## Viewing Pages

Published pages (`is_published=True`) are visible to everyone at their URL.

Draft pages (`is_published=False`) are only visible to logged-in users.

## Updating Pages

```python
await page_service.update_page(
    db_session,
    page_id=page.id,
    title="New Title",
    content="<p>Updated content</p>",
)
```

You can update: `slug`, `title`, `content`, `is_published`, `published_at`.

## Deleting Pages

```python
await page_service.delete_page(db_session, page_id=page.id)
```

## Listing Pages

```python
# All pages
pages = await page_service.list_pages(db_session)

# Published only
pages = await page_service.list_pages(db_session, published_only=True)

# With pagination
pages = await page_service.list_pages(db_session, limit=10, offset=0)
```

## Content Tips

Page content supports **Markdown** formatting, which is automatically rendered to HTML. This makes it easy to write rich content:

```markdown
# Welcome

Here's a **bold** statement and a [link](/about).

| Feature | Status |
|---------|--------|
| Tables  | Yes    |
| Links   | Yes    |
```

See the [Markdown Content Guide](markdown-content.md) for full syntax documentation.

You can also use HTML directly with the built-in CSS framework classes:

```html
<article>
    <header>
        <h2>Featured Service</h2>
    </header>
    <p>Description here.</p>
    <footer>
        <a href="/contact" role="button">Learn More</a>
    </footer>
</article>
```

See the [CSS Framework Reference](../reference/css-framework.md) for available styles.

## Markdown Content Negotiation

Page views support content negotiation via the `Accept` header. When a client requests `text/markdown`, the raw markdown content is returned instead of the rendered HTML page:

```bash
# Returns rendered HTML page (default)
curl http://localhost:8080/about

# Returns raw markdown content
curl -H "Accept: text/markdown" http://localhost:8080/about
```

This works for all page types — both standard pages (`/about`, `/contact`) and custom page types (`/post/hello-world`). The response uses `Content-Type: text/markdown`.

This is useful for:

- **API consumers** that need raw content for processing or display in their own UI
- **CLI tools** that want to fetch page content without HTML wrapping
- **Editor integrations** that need the source markdown for editing

## Next Steps

- [Markdown Content](markdown-content.md) - Full Markdown syntax guide
- [Custom Templates](custom-templates.md) - Create page-specific designs
- [CSS Framework](../reference/css-framework.md) - Style your content
