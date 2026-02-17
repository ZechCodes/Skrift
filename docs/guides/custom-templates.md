# Custom Templates

<span class="skill-badge intermediate">:material-star::material-star: Intermediate</span>

Learn how Skrift's WordPress-like template hierarchy works and how to create custom page designs.

## Template Hierarchy

Skrift resolves templates from most specific to least specific, similar to WordPress:

```
Request: /about
Tries:   page-about.html → page.html
```

This allows you to create:

- **Generic templates** (`page.html`) for most pages
- **Specific templates** (`page-about.html`) for individual pages

## How Resolution Works

The `Template` class in `skrift/lib/template.py` handles resolution:

```python
from skrift.lib.template import Template

# For URL /about
template = Template("page", "about")

# Tries these files in order:
# 1. page-about.html
# 2. page.html
```

## Creating Custom Templates

### 1. Default Page Template

The generic template for all pages:

**`templates/page.html`**

```html
{% extends "base.html" %}

{% block title %}{{ page.title }}{% endblock %}

{% block content %}
<article>
    <header>
        <h1>{{ page.title }}</h1>
    </header>

    <div class="page-content">
        {{ page.content | safe }}
    </div>
</article>
{% endblock %}
```

### 2. Specific Page Template

Create a custom template for the `/services` page:

**`templates/page-services.html`**

```html
{% extends "base.html" %}

{% block title %}Our Services{% endblock %}

{% block content %}
<article>
    <header>
        <h1>Our Services</h1>
        <p class="text-muted">Full-stack solutions for modern businesses</p>
    </header>

    {{ page.content | safe }}

    <section>
        <h2>Technologies We Use</h2>
        <ul>
            <li>Python & Litestar</li>
            <li>PostgreSQL</li>
            <li>Modern JavaScript</li>
        </ul>
    </section>
</article>
{% endblock %}
```

## Template Context

All page templates receive these variables:

| Variable | Type | Description |
|----------|------|-------------|
| `page` | Page | The page object from database |
| `path` | str | URL path (e.g., "about") |
| `user` | User \| None | Current logged-in user |
| `flash` | str \| None | Flash message from session |
| `now` | callable | Function returning current datetime |

### Using Context in Templates

```html
{% extends "base.html" %}

{% block content %}
<article>
    <h1>{{ page.title }}</h1>

    {% if user %}
        <p class="text-muted">
            Welcome back, {{ user.name }}!
        </p>
    {% endif %}

    {{ page.content | safe }}

    <footer>
        <small>Current path: /{{ path }}</small>
    </footer>
</article>
{% endblock %}
```

## Base Template Blocks

The `base.html` template provides these blocks:

| Block | Purpose |
|-------|---------|
| `title` | Page title (default: "Skrift") |
| `head` | Additional `<head>` content (CSS, meta tags) |
| `main_class` | Additional classes for `<main>` element |
| `content` | Main page content |
| `scripts` | JavaScript before `</body>` |

### Example: Adding Custom CSS

```html
{% extends "base.html" %}

{% block head %}
<style>
    .custom-hero {
        background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
        color: white;
        padding: var(--spacing-2xl);
        border-radius: var(--radius-lg);
    }
</style>
{% endblock %}

{% block content %}
<div class="custom-hero">
    <h1>{{ page.title }}</h1>
</div>
{% endblock %}
```

### Example: Error Page Styling

```html
{% extends "base.html" %}

{% block main_class %} error-page{% endblock %}

{% block content %}
<article>
    <header><h1>404</h1></header>
    <p>Page not found.</p>
    <footer>
        <a href="/" role="button">Return Home</a>
    </footer>
</article>
{% endblock %}
```

## Template Resolution Examples

| URL | Templates Tried |
|-----|-----------------|
| `/` | `index.html` |
| `/about` | `page-about.html` → `page.html` |
| `/services` | `page-services.html` → `page.html` |
| `/contact` | `page-contact.html` → `page.html` |

## Best Practices

### 1. Start Generic, Get Specific

Begin with `page.html`, then create specific templates as needed:

```
templates/
├── page.html              # Default for all pages
├── page-about.html        # About page
└── page-services.html     # Services page
```

### 2. Use Blocks for Variations

Instead of duplicating templates, use blocks and inheritance:

```html
{# page.html - base page template #}
{% extends "base.html" %}

{% block page_header %}
<header>
    <h1>{{ page.title }}</h1>
</header>
{% endblock %}

{% block content %}
{% block page_header %}{% endblock %}
{{ page.content | safe }}
{% endblock %}
```

```html
{# page-services.html - extends base page #}
{% extends "page.html" %}

{% block page_header %}
<header>
    <h1>{{ page.title }}</h1>
    <p class="text-muted">Professional solutions for your business</p>
</header>
{% endblock %}
```

### 3. Keep Templates DRY

Extract reusable components:

```html
{# _partials/page-header.html #}
<header>
    <h1>{{ title }}</h1>
    {% if subtitle %}<p class="text-muted">{{ subtitle }}</p>{% endif %}
</header>
```

```html
{# page.html #}
{% include "_partials/page-header.html" with context %}
```

## Themes

For bundling template and static overrides into a reusable package, see the [Theming](theming.md) guide. When a theme is active, its templates take priority over both project-level overrides and Skrift defaults:

```
themes/<active>/templates/  →  ./templates/  →  skrift/templates/
```

## Next Steps

- [Theming](theming.md) - Bundle templates into distributable themes
- [Custom Controllers](custom-controllers.md) - Add new routes
- [CSS Framework](../reference/css-framework.md) - Available styles
