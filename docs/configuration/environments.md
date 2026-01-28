# Environment-Specific Configuration

Skrift supports environment-specific configuration files, allowing different settings for development, staging, and production environments.

## How It Works

The `SKRIFT_ENV` environment variable controls which configuration file Skrift loads:

| SKRIFT_ENV Value | Config File |
|------------------|-------------|
| (unset) | `app.yaml` |
| `production` | `app.yaml` |
| `dev` | `app.dev.yaml` |
| `staging` | `app.staging.yaml` |
| `test` | `app.test.yaml` |

The environment name is normalized to lowercase, so `SKRIFT_ENV=DEV` and `SKRIFT_ENV=dev` both load `app.dev.yaml`.

## File Naming Convention

- **Production** (default): `app.yaml`
- **Other environments**: `app.{env}.yaml`

This keeps the production configuration at the standard path (`app.yaml`) for backward compatibility, while allowing environment-specific overrides.

## Usage

### Setting the Environment

=== "Shell"

    ```bash
    export SKRIFT_ENV=dev
    python -m skrift
    ```

=== "Docker"

    ```dockerfile
    ENV SKRIFT_ENV=staging
    ```

=== "systemd"

    ```ini
    [Service]
    Environment="SKRIFT_ENV=production"
    ```

### Example Configurations

=== "app.yaml (Production)"

    ```yaml
    controllers:
      - skrift.controllers.auth:AuthController
      - skrift.admin.controller:AdminController
      - skrift.controllers.web:WebController

    db:
      url: $DATABASE_URL
      pool_size: 10
      pool_overflow: 20

    auth:
      redirect_base_url: https://yourdomain.com
      providers:
        google:
          client_id: $GOOGLE_CLIENT_ID
          client_secret: $GOOGLE_CLIENT_SECRET
    ```

=== "app.dev.yaml (Development)"

    ```yaml
    controllers:
      - skrift.controllers.auth:AuthController
      - skrift.admin.controller:AdminController
      - skrift.controllers.web:WebController

    db:
      url: sqlite+aiosqlite:///./dev.db
      echo: true

    auth:
      redirect_base_url: http://localhost:8000
      providers:
        google:
          client_id: your-dev-client-id
          client_secret: your-dev-client-secret
    ```

## Programmatic Access

You can access the current environment and config path in your code:

```python
from skrift.config import get_environment, get_config_path

# Get current environment name
env = get_environment()  # e.g., "dev", "staging", "production"

# Get path to the config file
config_path = get_config_path()  # e.g., Path("app.dev.yaml")
```

## Best Practices

1. **Keep production as the default**: If `SKRIFT_ENV` is unset, Skrift loads `app.yaml`. This ensures production deployments work without additional configuration.

2. **Use environment variables for secrets**: Regardless of environment, keep secrets out of config files:

    ```yaml
    db:
      url: $DATABASE_URL
    ```

3. **Version control environment configs selectively**:
    - `app.yaml` (production template) - version control with placeholder values
    - `app.dev.yaml` - version control for shared development settings
    - `app.staging.yaml` - version control for staging

4. **Validate before deployment**: Ensure your environment-specific config file exists before starting the application.

## See Also

- [Environment Variables](environment.md) - Reference for environment variables
- [OAuth Providers](auth-providers.md) - OAuth configuration details
