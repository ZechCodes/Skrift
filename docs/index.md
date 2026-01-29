---
hide:
  - navigation
---

# Skrift

<div class="hero" markdown>
<div class="hero-content" markdown>

## From zero to secure, running site in 5 minutes

No boilerplate. No security gotchas. Just run `python -m skrift` and start building.

[Get Started](getting-started/quickstart.md){ .md-button .md-button--primary }
[View on GitHub](https://github.com/ZechCodes/Skrift){ .md-button }

</div>
</div>

<div class="grid cards" markdown>

-   :material-lightning-bolt:{ .lg .middle } **5-Minute Setup**

    ---

    Run `python -m skrift` and a setup wizard walks you through OAuth, database, and first admin user. No manual configuration required.

-   :material-cog-refresh:{ .lg .middle } **No-Restart Configuration**

    ---

    Edit `app.yaml` to add controllers, change routes, or update settings. Changes apply on next request—no server restart needed.

-   :material-palette:{ .lg .middle } **WordPress-like Templates**

    ---

    Hierarchical template resolution makes page-specific designs intuitive. Create `page-about.html` to override just the about page.

-   :material-shield-lock:{ .lg .middle } **OAuth with Automatic CSRF**

    ---

    OAuth state tokens are generated and verified automatically. No manual CSRF token handling required in your code.

-   :material-lock:{ .lg .middle } **Encrypted Sessions**

    ---

    Sessions use httponly, secure, and samesite cookies out of the box. Session data is encrypted, not just signed.

-   :material-shield-alert:{ .lg .middle } **Dev/Prod Isolation**

    ---

    Development-only features like dummy auth are blocked from production with a hard process kill—not a warning, an actual prevention.

</div>

## Quick Start

```bash
uv add skrift
python -m skrift
```

Open [http://localhost:8080](http://localhost:8080) and the setup wizard will guide you through:

1. **OAuth provider** - Connect Google, GitHub, or another provider
2. **Database** - SQLite for dev, PostgreSQL for production
3. **Admin user** - First login becomes the administrator

## Requirements

- Python 3.13+
- [uv](https://github.com/astral-sh/uv) package manager

## Next Steps

<div class="grid cards" markdown>

-   [**Quick Start Guide**](getting-started/quickstart.md)

    Walk through the setup wizard and create your first page.

-   [**How Skrift Works**](core-concepts/index.md)

    Understand the no-restart architecture and customization points.

-   [**Security Model**](core-concepts/security-model.md)

    Learn about the security features protecting your site.

-   [**Guides**](guides/index.md)

    Create pages, custom templates, and controllers.

</div>
