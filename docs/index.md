---
hide:
  - navigation
---

# Skrift

<div class="hero" markdown>
<div class="hero-content" markdown>

## A lightweight async Python CMS for crafting modern websites

Built on Litestar with Google OAuth, WordPress-like templates, and SQLAlchemy async support.

[Get Started](getting-started/quickstart.md){ .md-button .md-button--primary }
[View on GitHub](https://github.com/ZechCodes/Skrift){ .md-button }

</div>
</div>

<div class="grid cards" markdown>

-   :material-lightning-bolt:{ .lg .middle } **Fast & Async**

    ---

    Built on Litestar and SQLAlchemy async for high-performance, non-blocking I/O.

-   :material-palette:{ .lg .middle } **WordPress-like Templates**

    ---

    Hierarchical template resolution makes creating page-specific designs intuitive.

-   :material-shield-account:{ .lg .middle } **Secure Authentication**

    ---

    Google OAuth integration with encrypted session cookies out of the box.

-   :material-database:{ .lg .middle } **Flexible Database**

    ---

    SQLite for development, PostgreSQL for production. Alembic migrations included.

-   :material-cog:{ .lg .middle } **Dynamic Controllers**

    ---

    Load controllers from YAML configuration. Extend without modifying core code.

-   :material-theme-light-dark:{ .lg .middle } **Dark Mode Ready**

    ---

    Built-in CSS framework with automatic light/dark mode via system preference.

</div>

## Quick Start

```bash
uv add skrift
python -m skrift
```

Open [http://localhost:8080](http://localhost:8080) and the setup wizard will guide you through configuration.

## Requirements

- Python 3.13+
- [uv](https://github.com/astral-sh/uv) package manager

## Next Steps

<div class="grid cards" markdown>

-   [**Installation**](getting-started/installation.md)

    Multiple ways to install Skrift for your use case.

-   [**Quick Start**](getting-started/quickstart.md)

    Get your first site running.

-   [**Configuration**](configuration/index.md)

    Configure authentication, database, and more.

-   [**Guides**](guides/index.md)

    Learn to create pages, templates, and controllers.

</div>
