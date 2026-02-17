# Guides

Step-by-step guides for building and customizing your Skrift site.

## Building Your Site

<div class="grid cards" markdown>

-   :material-file-document:{ .lg .middle } **Creating Pages**

    ---

    Add content through the admin interface. Create pages, set slugs, and publish.

    [:octicons-arrow-right-24: Creating Pages](creating-pages.md)

-   :material-palette:{ .lg .middle } **Custom Templates**

    ---

    Customize how pages look with WordPress-style template hierarchy.

    [:octicons-arrow-right-24: Custom Templates](custom-templates.md)

</div>

## SEO & Discovery

<div class="grid cards" markdown>

-   :material-search-web:{ .lg .middle } **SEO Metadata**

    ---

    Configure meta descriptions, OpenGraph tags, and canonical URLs for search engines and social sharing.

    [:octicons-arrow-right-24: SEO Metadata](seo-metadata.md)

</div>

## Extending Functionality

<div class="grid cards" markdown>

-   :material-hook:{ .lg .middle } **Hooks and Filters**

    ---

    Extend Skrift with WordPress-like hooks and filters for custom behavior.

    [:octicons-arrow-right-24: Hooks and Filters](hooks-and-filters.md)

-   :material-code-braces:{ .lg .middle } **Custom Controllers**

    ---

    Add new routes, APIs, and functionality with Litestar controllers.

    [:octicons-arrow-right-24: Custom Controllers](custom-controllers.md)

-   :material-form-textbox:{ .lg .middle } **Forms**

    ---

    Build forms with CSRF protection, Pydantic validation, and template rendering.

    [:octicons-arrow-right-24: Forms](forms.md)

-   :material-layers-triple:{ .lg .middle } **Custom Middleware**

    ---

    Add request/response processing with ASGI middleware.

    [:octicons-arrow-right-24: Custom Middleware](custom-middleware.md)

-   :material-shield-lock:{ .lg .middle } **Protecting Routes**

    ---

    Secure your routes with authentication and authorization guards.

    [:octicons-arrow-right-24: Protecting Routes](protecting-routes.md)

-   :material-chart-timeline-variant:{ .lg .middle } **Observability**

    ---

    Add structured tracing and logging with Pydantic Logfire.

    [:octicons-arrow-right-24: Observability](observability.md)

</div>

## Guide Overview

| Guide | Focus | Prerequisites |
|-------|-------|---------------|
| [Creating Pages](creating-pages.md) | Content management | None |
| [Custom Templates](custom-templates.md) | Appearance | HTML basics |
| [SEO Metadata](seo-metadata.md) | Search & social | None |
| [Hooks and Filters](hooks-and-filters.md) | Extensibility | Python basics |
| [Custom Controllers](custom-controllers.md) | New routes | Python, async |
| [Forms](forms.md) | Form handling | Python basics |
| [Custom Middleware](custom-middleware.md) | Request processing | Python, ASGI |
| [Protecting Routes](protecting-routes.md) | Security | Python basics |
| [Observability](observability.md) | Tracing & logging | None |

## What You Can Build

After completing these guides, you'll be able to:

- Create and manage content pages
- Customize the look of specific pages with templates
- Add API endpoints and custom routes
- Add middleware for logging, rate limiting, CORS, and more
- Protect routes with role-based access control
- Build validated forms with CSRF protection and error handling
- Build features that integrate with Skrift's database and auth

## Related Sections

- [Core Concepts](../core-concepts/index.md) - How Skrift works
- [Admin](../admin/index.md) - Managing users and site settings
- [Reference](../reference/index.md) - Technical details
