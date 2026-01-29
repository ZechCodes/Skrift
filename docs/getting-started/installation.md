# Installation

Skrift can be installed in several ways depending on your needs.

## From PyPI

The simplest way to install Skrift:

=== "uv"

    ```bash
    uv add skrift
    ```

=== "pip"

    ```bash
    pip install skrift
    ```

## Development Setup

For contributing or local development:

```bash
# Clone the repository
git clone https://github.com/ZechCodes/Skrift.git
cd Skrift

# Install dependencies with uv
uv sync

# Or with pip
pip install -e .
```

### Development Dependencies

For documentation development:

```bash
# Install with docs dependencies
uv sync --group docs

# Serve documentation locally
zensical serve
```

## Verify Installation

After installation, verify Skrift is working:

```bash
# Start the development server
python -m skrift
```

## System Requirements

| Requirement | Minimum |
|-------------|---------|
| Python | 3.13+ |
| Operating System | Linux, macOS, Windows |
| Database | SQLite (included) or PostgreSQL 14+ |

## Next Steps

- [Quick Start](quickstart.md) - Get your first site running
- [Configuration](../core-concepts/configuration.md) - App settings and environment variables
