# Quick Start

Get your Skrift site running in minutes with secure defaults.

## Install and Run

```bash
# Install Skrift
uv add skrift

# Generate a secret key and save to .env
skrift secret --write .env

# Start the development server
skrift serve --reload
```

Open [http://localhost:8080](http://localhost:8080) to begin the setup wizard.

## Setup Wizard

The wizard walks you through four steps, generating a secure `app.yaml` configuration.

### Step 1: Database

Choose your database:

- **SQLite** - Simple file-based database, great for development and small sites
- **PostgreSQL** - Production-ready relational database

The wizard tests the connection and runs migrations automatically.

!!! tip "Security Note"
    For production, use PostgreSQL with credentials stored in environment variables. The wizard can configure `$DATABASE_URL` references.

### Step 2: Authentication

Configure OAuth providers for user login. Supported providers:

- Google
- GitHub
- Microsoft
- Discord
- Facebook
- Twitter/X

You'll need to create OAuth credentials with your chosen provider and enter the client ID and secret.

!!! tip "Security Note"
    OAuth is configured with automatic CSRF protection. State tokens are generated and verified for every login flow—you don't need to implement this yourself.

### Step 3: Site Settings

Configure your site's basic information:

- Site name
- Tagline
- Copyright holder

### Step 4: Admin Account

Log in with one of your configured OAuth providers to create your admin account. This account will have full access to manage the site.

!!! tip "Security Note"
    Your session is stored in an encrypted cookie with `httponly`, `secure`, and `samesite` flags set automatically.

## After Setup

Once the wizard completes:

1. `app.yaml` is created with your configuration
2. Database tables are initialized
3. You're logged in as the first administrator
4. Setup routes are locked—they won't be accessible again

You can now:

- Create pages through the admin interface
- Customize templates in the `templates/` directory
- Add custom controllers via `app.yaml`

## Development Mode

For local development, you can enable additional features:

```bash
export SKRIFT_ENV=dev
export DEBUG=true
skrift serve --reload
```

This loads `app.dev.yaml` (if it exists) and enables:

- Detailed error pages
- Template auto-reload
- Dummy authentication (for testing without OAuth)

See [Development](../development/index.md) for more on dev-only features.

## What's Secured Automatically

After setup, your site has:

| Feature | Status |
|---------|--------|
| Session encryption | Enabled (using `SECRET_KEY`) |
| CSRF protection | Enabled (OAuth state tokens) |
| Secure cookies | Enabled in production |
| Role-based access | First user is admin |

See [Security Model](../core-concepts/security-model.md) for details.

## Next Steps

<div class="grid cards" markdown>

-   [**How Skrift Works**](../core-concepts/index.md)

    Understand the no-restart architecture.

-   [**Creating Pages**](../guides/creating-pages.md)

    Add content to your site.

-   [**Custom Templates**](../guides/custom-templates.md)

    Customize the look of your pages.

-   [**Custom Controllers**](../guides/custom-controllers.md)

    Extend functionality with custom routes.

</div>
