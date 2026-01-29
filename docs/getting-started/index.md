# Getting Started

Welcome to Skrift! Get a secure, running site in minutes.

## Prerequisites

- **Python 3.13+**
- **uv** package manager (or pip)

## Quick Start

```bash
# Install Skrift
uv add skrift

# Set a secret key
export SECRET_KEY=$(python -c "import secrets; print(secrets.token_urlsafe(32))")

# Run it
python -m skrift
```

Open [http://localhost:8080](http://localhost:8080) and the setup wizard will guide you through:

1. **Database** - Configure SQLite or PostgreSQL
2. **Authentication** - Set up OAuth providers (Google, GitHub, etc.)
3. **Site Settings** - Name your site
4. **Admin Account** - Create your admin user via OAuth

That's it! Once complete, you'll have a fully configured site with:

- Encrypted session cookies
- CSRF protection for OAuth flows
- Role-based access control
- First user as administrator

## Learn More

<div class="grid cards" markdown>

-   :material-download:{ .lg .middle } **Installation Options**

    ---

    Alternative installation methods and development setup.

    [:octicons-arrow-right-24: Installation](installation.md)

-   :material-rocket-launch:{ .lg .middle } **Quick Start Details**

    ---

    Step-by-step walkthrough of the setup wizard.

    [:octicons-arrow-right-24: Quick Start](quickstart.md)

-   :material-shield-lock:{ .lg .middle } **Security Model**

    ---

    How Skrift protects your site automatically.

    [:octicons-arrow-right-24: Security Model](../core-concepts/security-model.md)

</div>
