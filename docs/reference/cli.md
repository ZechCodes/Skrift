# CLI Reference

Skrift provides a unified command-line interface for running the server, managing secrets, and handling database migrations.

## Installation

The `skrift` command is installed automatically when you install the package:

```bash
uv add skrift
```

## Commands

### skrift serve

Run the Skrift server.

```bash
skrift serve [OPTIONS]
```

**Options:**

| Option | Default | Description |
|--------|---------|-------------|
| `--host` | `127.0.0.1` | Host to bind to |
| `--port` | `8080` | Port to bind to |
| `--reload` | `false` | Enable auto-reload for development |
| `--workers` | `1` | Number of worker processes |
| `--log-level` | `info` | Logging level (`debug`, `info`, `warning`, `error`) |

**Examples:**

```bash
# Start development server with auto-reload
skrift serve --reload

# Production server on all interfaces with multiple workers
skrift serve --host 0.0.0.0 --workers 4

# Debug mode with verbose logging
skrift serve --reload --log-level debug
```

!!! note
    When `--reload` is enabled, `--workers` is ignored (forced to 1) since auto-reload doesn't work with multiple workers.

---

### skrift secret

Generate a secure secret key for session encryption.

```bash
skrift secret [OPTIONS]
```

**Options:**

| Option | Default | Description |
|--------|---------|-------------|
| `--write` | - | Write SECRET_KEY to a .env file |
| `--format` | `urlsafe` | Output format (`urlsafe`, `hex`, `base64`) |
| `--length` | `32` | Number of random bytes |

**Examples:**

```bash
# Generate and print a secret key
skrift secret

# Write directly to .env file
skrift secret --write .env

# Generate a longer key in hex format
skrift secret --format hex --length 64
```

!!! tip
    The `--write` option will create the file if it doesn't exist, or update an existing `SECRET_KEY` line if the file already exists.

---

### skrift db

Run database migrations via Alembic. This command passes all arguments through to Alembic.

```bash
skrift db [ALEMBIC_ARGS]
```

**Common Commands:**

| Command | Description |
|---------|-------------|
| `skrift db upgrade head` | Apply all pending migrations |
| `skrift db downgrade -1` | Rollback one migration |
| `skrift db current` | Show current database revision |
| `skrift db history` | Show migration history |
| `skrift db revision -m "description" --autogenerate` | Create a new migration |

**Examples:**

```bash
# Apply all migrations
skrift db upgrade head

# Rollback the last migration
skrift db downgrade -1

# Check current migration status
skrift db current

# Create a new migration after model changes
skrift db revision -m "add user preferences" --autogenerate
```

!!! note
    The `db` command automatically locates `alembic.ini` from either your project root or the Skrift package directory.

---

## Global Options

All commands support these global options:

| Option | Description |
|--------|-------------|
| `--version` | Show the version and exit |
| `--help` | Show help message and exit |

```bash
# Show version
skrift --version

# Show help for any command
skrift --help
skrift serve --help
skrift secret --help
skrift db --help
```

## Quick Start

```bash
# Generate a secret key and save to .env
skrift secret --write .env

# Start the development server
skrift serve --reload
```

Then open [http://localhost:8080](http://localhost:8080) to access the setup wizard.
