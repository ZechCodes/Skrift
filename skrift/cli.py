"""CLI commands for Skrift."""

import base64
import os
import re
import secrets
import sys
from pathlib import Path

import click


@click.group()
@click.version_option(package_name="skrift")
def cli():
    """Skrift - A lightweight async Python CMS."""
    pass


@cli.command()
@click.option("--host", default="127.0.0.1", help="Host to bind to")
@click.option("--port", default=8080, type=int, help="Port to bind to")
@click.option("--reload", is_flag=True, help="Enable auto-reload for development")
@click.option("--workers", default=1, type=int, help="Number of worker processes")
@click.option(
    "--log-level",
    default="info",
    type=click.Choice(["debug", "info", "warning", "error"]),
    help="Logging level",
)
def serve(host, port, reload, workers, log_level):
    """Run the Skrift server."""
    import uvicorn

    uvicorn.run(
        "skrift.asgi:app",
        host=host,
        port=port,
        reload=reload,
        workers=workers if not reload else 1,
        log_level=log_level,
    )


@cli.command()
@click.option(
    "--write",
    type=click.Path(),
    default=None,
    help="Write SECRET_KEY to a .env file",
)
@click.option(
    "--format",
    "fmt",
    default="urlsafe",
    type=click.Choice(["urlsafe", "hex", "base64"]),
    help="Output format for the secret key",
)
@click.option("--length", default=32, type=int, help="Number of random bytes")
def secret(write, fmt, length):
    """Generate a secure secret key."""
    # Generate key based on format
    if fmt == "urlsafe":
        key = secrets.token_urlsafe(length)
    elif fmt == "hex":
        key = secrets.token_hex(length)
    else:  # base64
        key = base64.b64encode(secrets.token_bytes(length)).decode("ascii")

    if write:
        env_path = Path(write)
        env_content = ""

        # Read existing content if file exists
        if env_path.exists():
            env_content = env_path.read_text()

        # Update or add SECRET_KEY
        secret_key_pattern = re.compile(r"^SECRET_KEY=.*$", re.MULTILINE)
        new_line = f"SECRET_KEY={key}"

        if secret_key_pattern.search(env_content):
            # Replace existing SECRET_KEY
            env_content = secret_key_pattern.sub(new_line, env_content)
        else:
            # Add SECRET_KEY at the end
            if env_content and not env_content.endswith("\n"):
                env_content += "\n"
            env_content += new_line + "\n"

        env_path.write_text(env_content)
        click.echo(f"SECRET_KEY written to {env_path}")
    else:
        click.echo(key)


@cli.command(
    context_settings=dict(
        ignore_unknown_options=True,
        allow_extra_args=True,
    )
)
@click.pass_context
def db(ctx):
    """Run database migrations via Alembic.

    \b
    Examples:
        skrift db upgrade head     # Apply all migrations
        skrift db downgrade -1     # Rollback one migration
        skrift db current          # Show current revision
        skrift db history          # Show migration history
        skrift db revision -m "description" --autogenerate  # Create new migration
    """
    from alembic.config import main as alembic_main

    # Always run from the project root (where app.yaml and .env are)
    # This ensures database paths like ./app.db resolve correctly
    project_root = Path.cwd()
    if not (project_root / "app.yaml").exists():
        # If not in project root, try parent directory
        project_root = Path(__file__).parent.parent
    os.chdir(project_root)

    # Find alembic.ini - check project root first, then skrift package directory
    alembic_ini = project_root / "alembic.ini"
    if not alembic_ini.exists():
        skrift_dir = Path(__file__).parent
        alembic_ini = skrift_dir / "alembic.ini"

        if not alembic_ini.exists():
            click.echo("Error: Could not find alembic.ini", err=True)
            click.echo("Make sure you're running from the project root directory.", err=True)
            sys.exit(1)

    # Build argv for alembic: ['-c', '/path/to/alembic.ini', ...extra_args]
    alembic_argv = ["-c", str(alembic_ini)] + ctx.args

    sys.exit(alembic_main(alembic_argv))


if __name__ == "__main__":
    cli()
