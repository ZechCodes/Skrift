# Quick Start

Get your Skrift site running in minutes.

## Install and Run

```bash
# Install Skrift
uv add skrift

# Start the server
python -m skrift
```

Open [http://localhost:8080](http://localhost:8080) to begin the setup wizard.

## Setup Wizard

The wizard walks you through four steps:

### Step 1: Database

Choose your database:

- **SQLite** - Simple file-based database, great for development and small sites
- **PostgreSQL** - Production-ready relational database

The wizard tests the connection and runs migrations automatically.

### Step 2: Authentication

Configure OAuth providers for user login. Supported providers:

- Google
- GitHub
- Microsoft
- Discord
- Facebook
- Twitter/X

You'll need to create OAuth credentials with your chosen provider and enter the client ID and secret.

### Step 3: Site Settings

Configure your site's basic information:

- Site name
- Tagline
- Copyright holder

### Step 4: Admin Account

Log in with one of your configured OAuth providers to create your admin account. This account will have full access to manage the site.

## After Setup

Once the wizard completes, your site is ready. You can:

- Create pages through the admin interface
- Customize templates in the `templates/` directory
- Add custom controllers via `app.yaml`

## Next Steps

<div class="grid cards" markdown>

-   [**Creating Pages**](../guides/creating-pages.md)

    Add content to your site.

-   [**Custom Templates**](../guides/custom-templates.md)

    Customize the look of your pages.

-   [**Custom Controllers**](../guides/custom-controllers.md)

    Extend functionality with custom routes.

</div>
