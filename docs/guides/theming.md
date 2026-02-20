# Theming

<span class="skill-badge intermediate">:material-star::material-star: Intermediate</span>

Themes let you bundle template and static file overrides into a self-contained directory. When a theme is active, its files take top priority in the resolution order — ahead of your project's `./templates/` overrides and Skrift's built-in defaults.

## Theme Directory Structure

Place themes in a `themes/` directory in your project root:

```
my-site/
├── app.yaml
├── themes/
│   └── my-theme/
│       ├── theme.yaml          # optional metadata
│       ├── templates/           # required — template overrides
│       │   ├── base.html
│       │   └── page.html
│       ├── static/              # optional — static file overrides
│       │   └── css/
│       │       └── site.css
│       └── screenshot.png       # optional — preview for admin UI
├── templates/                   # project-level overrides (still work)
└── ...
```

A theme directory is valid if it contains a `templates/` subdirectory.

### theme.yaml

The optional metadata file provides display info for the setup wizard and admin settings:

```yaml
name: My Theme
description: A clean, minimal theme for blogs
version: "1.0"
author: Jane Smith
```

All fields are optional. If omitted, the directory name is used as the display name.

## Resolution Order

When a theme is active, the three-tier resolution order is:

| Priority | Directory | Source |
|----------|-----------|--------|
| 1 (highest) | `themes/<active>/templates/` | Active theme |
| 2 | `./templates/` | Project-level overrides |
| 3 (lowest) | `skrift/templates/` | Built-in defaults |

Static files follow the same pattern:

| Priority | Directory | Source |
|----------|-----------|--------|
| 1 (highest) | `themes/<active>/static/` | Active theme |
| 2 | `./static/` | Project-level overrides |
| 3 (lowest) | `skrift/static/` | Built-in defaults |

Without a theme, the order is the same as before (project overrides, then package defaults).

## Scope

Themes only affect **frontend/public templates**. Admin templates (`admin/*`) and setup templates (`setup/*`) are never overridden by themes — they use direct `TemplateResponse("admin/...")` paths that bypass theme resolution.

## Selecting a Theme

### During Setup

If a `themes/` directory with valid themes exists when running the setup wizard, an additional "Theme" step appears between "Site Settings" and "Admin Account". Select a theme or choose "Default (no theme)" to skip.

### In app.yaml

Set a default theme in your configuration file:

```yaml
theme: my-theme
```

This theme is used when the database hasn't been configured yet or is unavailable. The admin UI theme setting (stored in the database) overrides this value when present.

### In Admin Settings

After setup, go to **Admin > Settings**. When themes are available, a theme dropdown appears in the site settings form. Changes take effect immediately — no server restart required.

## Creating a Theme

### 1. Create the directory structure

```bash
mkdir -p themes/my-theme/templates themes/my-theme/static/css
```

### 2. Override templates

Copy and modify any template from `skrift/templates/`:

**`themes/my-theme/templates/base.html`**
```html
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{% block title %}{{ site_name() }}{% endblock %}</title>
    <link rel="stylesheet" href="{{ static_url('css/skrift.css') }}">
    <link rel="stylesheet" href="{{ static_url('css/mytheme.css') }}">
    {% block head %}{% endblock %}
</head>
<body>
    <main>{% block content %}{% endblock %}</main>
    {% block scripts %}{% endblock %}
</body>
</html>
```

### 3. Add static files

**`themes/my-theme/static/css/mytheme.css`**
```css
:root {
    --sk-color-accent: #e63946;
    --sk-color-accent-text: #fff;
    --sk-font-heading: 'Georgia', serif;
}
```

### 4. Add metadata

**`themes/my-theme/theme.yaml`**
```yaml
name: My Theme
description: A bold red accent theme
version: "1.0"
author: Your Name
```

### 5. Add a preview (optional)

Take a screenshot and save it as `themes/my-theme/screenshot.png`. This is displayed in the setup wizard and could be used by admin UI extensions.

## Per-Request Theme Switching

The `RESOLVE_THEME` filter hook lets plugins override the active theme on a per-request basis. This enables features like user theme preferences or A/B testing.

### Example: Per-User Theme Preference

```python
from skrift.lib.hooks import filter

@filter("resolve_theme")
async def user_theme_preference(theme_name, request):
    """Let users choose their theme via session."""
    return request.session.get("user_theme", theme_name)
```

### Example: Query Parameter Override

```python
from skrift.lib.hooks import filter

@filter("resolve_theme")
async def theme_preview(theme_name, request):
    """Allow ?theme=name for previewing themes."""
    preview = request.query_params.get("theme")
    if preview:
        from skrift.lib.theme import get_theme_info
        if get_theme_info(preview):
            return preview
    return theme_name
```

The filter receives `(theme_name: str, request: Request)` and must return a theme name string (or empty string for no theme).

## Theme Discovery API

The `skrift.lib.theme` module provides programmatic access to theme information:

```python
from skrift.lib.theme import (
    themes_available,    # bool — is themes/ dir present with valid themes?
    discover_themes,     # list[ThemeInfo] — all valid themes
    get_theme_info,      # ThemeInfo | None — lookup by directory name
    get_themes_dir,      # Path — the themes/ directory path
)
```

### ThemeInfo Fields

| Field | Type | Description |
|-------|------|-------------|
| `directory_name` | str | Directory name (used as the theme identifier) |
| `name` | str | Display name (from theme.yaml or directory name) |
| `description` | str | Theme description |
| `version` | str | Theme version |
| `author` | str | Theme author |
| `templates_dir` | Path | Path to the theme's templates/ directory |
| `static_dir` | Path \| None | Path to static/ (None if not present) |
| `screenshot` | Path \| None | Path to screenshot.png (None if not present) |

## Template Globals

Two theme-related globals are available in all templates:

| Global | Type | Description |
|--------|------|-------------|
| `active_theme()` | str | Currently active theme name (empty if none) |
| `themes_available()` | bool | Whether any themes exist |

```html
{% if active_theme() %}
<meta name="theme" content="{{ active_theme() }}">
{% endif %}
```

## How It Works Internally

1. The active theme name is resolved with the following priority: DB `site_theme` setting > `app.yaml` `theme` key > no theme (Skrift defaults)
2. On app startup, `get_template_directories()` and `get_static_directories()` prepend the theme's directories to the search path
3. The Jinja2 `FileSystemLoader` searches directories in order, so theme templates are found first
4. When the theme is changed in admin settings, `update_template_directories()` updates the Jinja loader's search path in-place — no restart needed
5. If the database is unavailable, the `app.yaml` theme ensures the site still renders with the correct theme
6. For page routes, the `RESOLVE_THEME` filter hook can override the theme per-request before template resolution

## Next Steps

- [Custom Templates](custom-templates.md) - Template hierarchy and overrides
- [Hooks and Filters](hooks-and-filters.md) - The `RESOLVE_THEME` filter hook
- [CSS Framework](../reference/css-framework.md) - Available CSS custom properties to override
