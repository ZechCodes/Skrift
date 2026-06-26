---
name: skrift-frontend
description: "Skrift frontend — templates, themes, static assets, JS/CSS, CSP nonces, and cache-busting."
---

# Skrift Frontend

Templates, themes, static assets, and security for the Skrift frontend.

## Template Resolution

WordPress-style hierarchy with fallbacks and optional theme support:

```python
from skrift.template import Template

# Tries: page-about.html -> page.html
template = Template("page", "about")

# Tries: post-news-2024.html -> post-news.html -> post.html
template = Template("post", "news", "2024")

# Render with theme
return template.render(TEMPLATE_DIR, theme_name="my-theme", page=page)
```

### Search Order

With an active theme:
1. `themes/<active>/templates/` (active theme — highest priority)
2. `./templates/` (project-level overrides)
3. `skrift/templates/` (package defaults — lowest priority)

Without a theme, tiers 2 and 3 apply.

### Template Globals

| Global | Type | Description |
|--------|------|-------------|
| `now()` | datetime | Current datetime |
| `site_name()` | str | Site name from settings |
| `site_tagline()` | str | Site tagline from settings |
| `site_copyright_holder()` | str | Copyright holder |
| `site_copyright_start_year()` | str | Copyright start year |
| `active_theme()` | str | Current theme name (empty if none) |
| `themes_available()` | bool | Whether `themes/` has valid themes |
| `static_url(path)` | str | Cache-busted static URL |
| `theme_url(path)` | str | Theme-aware static URL |
| `favicon_url()` | str | Cached site favicon URL (internal `/storage` URL) |
| `csp_nonce()` | str | Current request's CSP nonce |

Filters: `markdown` — renders markdown to HTML; `sized('name')` — appends
`?size=name` for on-demand image sizing (see [Asset URLs & Image
Sizing](#asset-urls-image-sizing)).

## Themes

### Directory Structure

```
themes/
  my-theme/
    theme.yaml          # optional metadata (name, description, version, author)
    templates/           # required — template overrides
    static/              # optional — static file overrides
    screenshot.png       # optional — preview for admin UI
```

A theme directory is valid if it has a `templates/` subdirectory.

### Theme Discovery API

```python
from skrift.lib.theme import (
    ThemeInfo,           # dataclass: directory_name, name, description, version, author, templates_dir, static_dir, screenshot
    discover_themes,     # -> list[ThemeInfo]: all valid themes, sorted by name
    get_theme_info,      # (name: str) -> ThemeInfo | None
    themes_available,    # -> bool: themes/ dir has at least one valid theme
)
```

### Active Theme

The active theme is stored as `site_theme` in the settings table. Resolution order: DB `site_theme` > app.yaml `theme` > no theme.

```yaml
theme: my-theme  # fallback when DB is unavailable
```

```python
from skrift.db.services.setting_service import get_cached_site_theme
theme_name = get_cached_site_theme()
```

### Per-Request Theme Switching

The `RESOLVE_THEME` filter hook allows overriding the theme per-request:

```python
from skrift.hooks import filter

@filter("resolve_theme")
async def user_theme(theme_name, request):
    return request.session.get("user_theme", theme_name)
```

Applied in `WebController` before template resolution. See `/skrift-events` for hooks documentation.

### Creating a Theme

1. Create `themes/<name>/templates/`
2. Override templates from `skrift/templates/` (e.g., `base.html`, `page.html`, `index.html`)
3. Optionally add `static/` for CSS/JS overrides
4. Optionally add `theme.yaml` for metadata
5. Optionally add `screenshot.png` for admin preview

Themes only affect frontend templates. Admin and setup templates are unaffected.

## Static Assets & Cache Busting

Static files follow the same three-tier resolution as templates: theme → project → package.

```html
<!-- In templates -->
<link rel="stylesheet" href="{{ static_url('css/site.css') }}">
<script src="{{ static_url('js/app.js') }}"></script>

<!-- Theme-specific static files -->
<link rel="stylesheet" href="{{ theme_url('css/theme.css') }}">
```

`static_url(path)` appends `?h=<hash>` for cache-busting. The hash is computed at startup by `create_static_hasher()` in `skrift/app_factory.py`.

`theme_url(path)` resolves via the active theme's `static/` directory.

## Asset URLs & Image Sizing

Uploaded assets are served at `/storage/{store}/{key}` by `StorageFilesMiddleware`,
which works across any storage backend (local, S3, custom). Images can be
requested at a named size with `?size=<name>`; variants are generated lazily,
cached back into the backend, and served from there (inline for local, `302` to
the CDN for remote). Sizes live in `skrift.lib.imaging.IMAGE_SIZES`: `icon`
(64×64), `thumb` (200×200), `small` (400w), `medium` (800w), `cover` (1200w),
`og` (1200×630). Resizing never upscales and preserves the original format.

### Async helpers are code-side, not template-side

Templates render **synchronously**, so the async URL helpers cannot be awaited
in a template (they return a coroutine). Resolve in the controller, pass strings
into the context:

```python
from skrift.db.services.asset_service import get_asset_url, image_url, internal_asset_url

storage = request.app.state.storage_manager
original = await get_asset_url(storage, asset)        # backend-native (CDN/local) URL
thumb = await image_url(storage, asset, "thumb")      # lazy, backend-aware sized URL
internal = internal_asset_url(asset.store, asset.key) # always-internal /storage URL
```

`image_url` returns the backend's direct URL when no size is given or the variant
already exists (warm path → straight to the CDN, no Skrift round-trip), and the
internal `/storage/{store}/{key}?size=…` URL when the variant is missing (cold
path → generate + redirect on first hit).

### In templates: precomputed context + the `sized` filter

Public page templates receive pre-resolved strings:

| Context variable | Contents |
|------------------|----------|
| `asset_urls` | `{asset_id: original_url}` for every attached asset |
| `asset_image_urls` | `{asset_id: medium_url}` for image assets (via `image_url`) |
| `featured_image_url` | Internal `/storage` URL for the featured asset (sized for og:image) |
| `featured_cover_url` | Pre-resolved `cover` URL for on-page display |

```html
<img src="{{ asset_image_urls[asset.id | string] }}" alt="{{ asset.alt_text }}">
<img src="{{ featured_cover_url }}" alt="{{ page.title }}">
<a href="{{ asset_urls[asset.id | string] }}">Open original</a>
```

The `sized('name')` filter is for URLs sized directly in-template (favicon,
og:image). It only appends `?size=`, so it triggers resizing **only** on an
internal `/storage` URL — appending to a CDN URL does nothing. Built-in
controllers pass internal URLs for the favicon and og:image for this reason:

```html
<link rel="icon" href="{{ favicon_url() | sized('icon') }}">
<meta property="og:image" content="{{ og_meta.image | sized('og') }}">
```

Full reference: [Media & Storage guide](../../../docs/guides/media-and-storage.md).

## CSP Nonces

The `SecurityHeadersMiddleware` generates a per-request nonce and injects it into the CSP header. Use nonces for inline scripts and styles:

```html
<!-- Inline script with nonce -->
<script nonce="{{ csp_nonce() }}">
    console.log("This is allowed by CSP");
</script>

<!-- Inline style with nonce -->
<style nonce="{{ csp_nonce() }}">
    .custom { color: red; }
</style>
```

### How It Works

1. `SecurityHeadersMiddleware` generates `secrets.token_urlsafe(16)` per request
2. Stores in `csp_nonce_var` ContextVar (accessible in templates via `csp_nonce()` global)
3. Replaces `'unsafe-inline'` in `style-src` with `'nonce-{value}'`
4. Appends `'nonce-{value}'` to `script-src`

Configure CSP in `app.yaml`:

```yaml
security_headers:
  content_security_policy: "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'"
  csp_nonce: true  # default: true
```

### Programmatic Access

```python
from skrift.middleware.security import csp_nonce_var

nonce = csp_nonce_var.get("")  # empty string if not in request context
```

## Form Templates

For form rendering and CSRF tokens, see `/skrift-forms`.

## Key Files

| File | Purpose |
|------|---------|
| `skrift/lib/template.py` | `Template` class with theme-aware resolution |
| `skrift/lib/theme.py` | Theme discovery, `ThemeInfo` dataclass, metadata parsing |
| `skrift/app_factory.py` | Directory builders, `create_static_hasher()`, template config |
| `skrift/middleware/security.py` | `SecurityHeadersMiddleware`, `csp_nonce_var` |
| `skrift/middleware/storage.py` | `StorageFilesMiddleware` — asset serving + on-demand sizing |
| `skrift/lib/imaging.py` | `IMAGE_SIZES`, `resize_image()`, variant helpers |
| `skrift/db/services/asset_service.py` | `get_asset_url`, `image_url`, `internal_asset_url` |
| `skrift/db/services/setting_service.py` | `SITE_THEME_KEY`, `get_cached_site_theme()` |
| `skrift/lib/hooks.py` | `RESOLVE_THEME` filter hook constant |
| `skrift/controllers/web.py` | Per-request theme resolution |
| `skrift/templates/` | Package default templates |
| `skrift/static/` | Package default static assets |
