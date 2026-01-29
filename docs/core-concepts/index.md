# How Skrift Works

Skrift is designed around three goals: get started fast, stay secure, and customize without limits. This guide explains the architecture that makes this possible.

## The Setup Wizard

When you first run Skrift, there's no configuration file. Instead of failing with an error, Skrift starts a setup wizard that walks you through configuration:

```
$ python -m skrift
INFO:     Uvicorn running on http://localhost:8080
```

Visit [http://localhost:8080](http://localhost:8080) and the wizard guides you through:

1. **OAuth Provider** - Connect Google, GitHub, or another provider for authentication
2. **Database** - Choose SQLite for development or PostgreSQL for production
3. **Admin User** - Log in with your OAuth provider to become the first administrator

The wizard generates your `app.yaml` configuration file automatically. No manual YAML editing required to get started.

## No-Restart Architecture

Traditional web frameworks require a server restart when you change configuration. Skrift doesn't.

### How It Works

Skrift uses a dispatcher pattern with lazy app creation:

```
┌─────────────────────────────────────────┐
│              AppDispatcher              │
├──────────────────┬──────────────────────┤
│    Setup App     │      Main App        │
│  (always ready)  │  (created on demand) │
└──────────────────┴──────────────────────┘
```

1. **Setup App** is always available to handle the setup wizard
2. **Main App** is created lazily when first needed, reading the current `app.yaml`
3. On each request, the dispatcher checks which app should handle it

This means you can:

- Edit `app.yaml` to add a new controller
- Save the file
- The next request uses the new configuration

No restart. No downtime. No "please wait while the server reloads" messages.

### When Main App Is Recreated

The main app is created once and reused. It's recreated when:

- The server restarts (obviously)
- Setup completes for the first time
- You explicitly trigger a reload

For most configuration changes, you'll see them on the next request without any recreation needed because controllers are loaded dynamically.

## Configuration Flow

Configuration in Skrift follows a predictable pattern:

```
┌──────────────┐    ┌──────────────┐    ┌──────────────┐
│ Environment  │ -> │   app.yaml   │ -> │   Settings   │
│  Variables   │    │   (YAML)     │    │   (Python)   │
└──────────────┘    └──────────────┘    └──────────────┘
        │                  │                    │
        │                  ▼                    │
        │         $VAR interpolation           │
        └──────────────────┼───────────────────┘
                           ▼
                    Final Config
```

1. **Environment Variables** store secrets (`SECRET_KEY`, `GOOGLE_CLIENT_SECRET`)
2. **app.yaml** references them with `$VAR_NAME` syntax
3. **Settings** validates and provides typed access to configuration

This separation means:

- `app.yaml` can be committed to version control
- Secrets stay in environment variables
- Different environments use different env vars, same `app.yaml` structure

See [Configuration](configuration.md) for details.

## Customization Points

Skrift provides three levels of customization, from simple to advanced:

### 1. Templates (No Code)

Override any template by creating a file with the right name:

```
templates/
├── page-about.html      # Custom about page
├── page-contact.html    # Custom contact page
└── base.html            # Custom base layout
```

Template hierarchy follows WordPress conventions. See [Custom Templates](../guides/custom-templates.md).

### 2. Dynamic Controllers (YAML)

Add routes without touching Python code by listing controllers in `app.yaml`:

```yaml
controllers:
  - myapp.controllers:BlogController
  - myapp.api:APIController
```

Controllers are imported and mounted automatically. See [Custom Controllers](../guides/custom-controllers.md).

### 3. Code Extensions (Python)

For full control, extend Skrift with Python:

- Custom auth guards for fine-grained access control
- Database models with SQLAlchemy
- Background tasks and scheduled jobs
- Custom middleware

## Security Integration

Security isn't a separate concern in Skrift—it's woven into every layer:

| Layer | Security Feature |
|-------|------------------|
| Setup | Validates OAuth before allowing admin access |
| Sessions | Encrypted cookies with httponly, secure, samesite |
| OAuth | Automatic CSRF state tokens |
| Configuration | Environment variable interpolation for secrets |
| Controllers | Built-in auth guards and permission system |
| Production | Hard kill switch for dev-only features |

See [Security Model](security-model.md) for the complete picture.

## Next Steps

<div class="grid cards" markdown>

-   [**Security Model**](security-model.md)

    Understand how Skrift protects your site automatically.

-   [**Configuration**](configuration.md)

    Learn about app.yaml structure and environment-specific configs.

-   [**Quick Start**](../getting-started/quickstart.md)

    Follow along with the setup wizard step by step.

</div>
