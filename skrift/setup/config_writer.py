"""Safe app.yaml configuration writer using ruamel.yaml to preserve comments."""

import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

from skrift.config import (
    get_auth_method_configs,
    get_auth_provider_configs,
    get_config_path as _get_config_path,
)

# Default app.yaml structure
DEFAULT_CONFIG = {
    "controllers": [
        "skrift.controllers.auth:AuthController",
        "skrift.admin.controller:AdminController",
        "skrift.controllers.web:WebController",
    ],
    "db": {
        "url": "sqlite+aiosqlite:///./app.db",
        "pool_size": 5,
        "pool_overflow": 10,
        "pool_timeout": 30,
        "pool_pre_ping": True,
        "echo": False,
    },
    "auth": {
        "redirect_base_url": "http://localhost:8000",
        "methods": {},
        "providers": {},
    },
}


def get_config_path() -> Path:
    """Get the path to the current environment's config file."""
    return _get_config_path()


def backup_config() -> Path | None:
    """Create a backup of app.yaml if it exists.

    Returns:
        Path to backup file or None if no backup was created
    """
    config_path = get_config_path()
    if not config_path.exists():
        return None

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = config_path.with_suffix(f".yaml.backup.{timestamp}")
    shutil.copy2(config_path, backup_path)
    return backup_path


def load_config() -> dict[str, Any]:
    """Load current config or return default structure."""
    yaml = YAML()
    yaml.preserve_quotes = True

    config_path = get_config_path()
    if not config_path.exists():
        return DEFAULT_CONFIG.copy()

    with open(config_path, "r") as f:
        config = yaml.load(f)
        return config if config else DEFAULT_CONFIG.copy()


def save_config(config: dict[str, Any]) -> None:
    """Save configuration to app.yaml.

    Args:
        config: Configuration dictionary to save
    """
    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.indent(mapping=2, sequence=4, offset=2)

    config_path = get_config_path()

    # Validate YAML can be serialized before writing
    from io import StringIO

    test_stream = StringIO()
    yaml.dump(config, test_stream)

    # Write to file
    with open(config_path, "w") as f:
        yaml.dump(config, f)


def update_database_config(
    db_type: str,
    url: str | None = None,
    host: str | None = None,
    port: int | None = None,
    database: str | None = None,
    username: str | None = None,
    password: str | None = None,
    use_env_vars: dict[str, bool] | None = None,
) -> dict[str, Any]:
    """Update database configuration in app.yaml.

    Args:
        db_type: Either "sqlite" or "postgresql"
        url: Direct URL (for sqlite file path)
        host: PostgreSQL host
        port: PostgreSQL port
        database: PostgreSQL database name
        username: PostgreSQL username
        password: PostgreSQL password
        use_env_vars: Dict mapping field names to whether they should use env vars

    Returns:
        Updated configuration
    """
    backup_config()
    config = load_config()

    if "db" not in config:
        config["db"] = {}

    use_env_vars = use_env_vars or {}

    if db_type == "sqlite":
        file_path = url or "./app.db"
        if use_env_vars.get("url"):
            config["db"]["url"] = f"${file_path}"
        else:
            config["db"]["url"] = f"sqlite+aiosqlite:///{file_path}"
    else:
        # PostgreSQL
        if use_env_vars.get("url"):
            # Use a single env var for the whole URL
            config["db"]["url"] = f"${url}"
        else:
            # Build URL from components
            host_str = f"${host}" if use_env_vars.get("host") else host
            port_str = f"${port}" if use_env_vars.get("port") else port
            db_str = f"${database}" if use_env_vars.get("database") else database
            user_str = f"${username}" if use_env_vars.get("username") else username
            pass_str = f"${password}" if use_env_vars.get("password") else password

            # For env vars in components, we need to store them as env var references
            if any(use_env_vars.values()):
                # Store the URL with env var placeholders
                config["db"]["url"] = f"$DATABASE_URL"
            else:
                config["db"]["url"] = (
                    f"postgresql+asyncpg://{user_str}:{pass_str}@{host_str}:{port_str}/{db_str}"
                )

    save_config(config)
    return config


def update_auth_config(
    redirect_base_url: str,
    providers: dict[str, dict[str, Any]] | None = None,
    methods: dict[str, dict[str, Any]] | None = None,
    use_env_vars: dict[str, dict[str, bool]] | None = None,
) -> dict[str, Any]:
    """Update authentication configuration in app.yaml.

    Args:
        redirect_base_url: Base URL for OAuth callbacks
        providers: Legacy dict of provider configs {provider: {client_id, client_secret, ...}}
        methods: Dict of auth method configs {method: {type, ...}}
        use_env_vars: Dict of {provider: {field: use_env_var}} for env var toggles

    Returns:
        Updated configuration
    """
    backup_config()
    config = load_config()

    if "auth" not in config:
        config["auth"] = {}

    config["auth"]["redirect_base_url"] = redirect_base_url

    if "methods" not in config["auth"]:
        config["auth"]["methods"] = {}
    if "providers" not in config["auth"]:
        config["auth"]["providers"] = {}

    use_env_vars = use_env_vars or {}
    methods = methods or {}

    if not methods and providers:
        methods = get_auth_method_configs({"providers": providers})

    processed_methods: dict[str, dict[str, Any]] = {}

    for method_key, method_config in methods.items():
        method_env_vars = use_env_vars.get(method_key, {})
        processed_config: dict[str, Any] = {}

        for field, value in method_config.items():
            if method_env_vars.get(field):
                processed_config[field] = f"${value}"
            else:
                processed_config[field] = value

        method_type = processed_config.get("type", "") or "oauth"

        if method_type == "oauth":
            from skrift.setup.providers import get_provider_info

            provider_type = processed_config.get("provider", "") or method_key
            provider_info = get_provider_info(provider_type)
            if provider_info and "scopes" not in processed_config:
                processed_config["scopes"] = provider_info.scopes

        processed_methods[method_key] = processed_config
        config["auth"]["methods"][method_key] = processed_config

    for provider_key, provider_config in get_auth_provider_configs({"methods": processed_methods}).items():
        config["auth"]["providers"][provider_key] = provider_config

    save_config(config)
    return config


def get_configured_providers() -> list[str]:
    """Get list of providers currently configured in app.yaml."""
    config = load_config()
    auth = config.get("auth", {})
    providers = get_auth_provider_configs(auth)
    return list(providers.keys())
