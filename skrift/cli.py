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
@click.option(
    "--subdomain",
    default=None,
    help="Serve only this subdomain site (for local multi-site testing)",
)
def serve(host, port, reload, workers, log_level, subdomain):
    """Run the Skrift server."""
    import asyncio
    import signal

    from hypercorn.asyncio import serve as hypercorn_serve
    from hypercorn.config import Config

    if subdomain:
        os.environ["SKRIFT_SUBDOMAIN"] = subdomain
        click.echo(f"Serving subdomain '{subdomain}' on {host}:{port}")

    config = Config()
    config.application_path = "skrift.asgi:app"
    config.bind = [f"{host}:{port}"]
    config.workers = 1 if reload else workers
    config.loglevel = log_level.upper()
    config.include_server_header = False

    if reload:
        config.use_reloader = True
        from hypercorn.run import run
        run(config)
        return

    from skrift.asgi import app

    shutdown_event = asyncio.Event()

    loop = asyncio.new_event_loop()
    loop.add_signal_handler(signal.SIGINT, shutdown_event.set)
    loop.add_signal_handler(signal.SIGTERM, shutdown_event.set)
    try:
        loop.run_until_complete(
            hypercorn_serve(app, config, shutdown_trigger=shutdown_event.wait)
        )
    finally:
        loop.close()


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


def _db_init(project_root: Path) -> None:
    """Initialize a user migrations directory with versions/ and script.py.mako."""
    import shutil

    migrations_dir = project_root / "migrations" / "versions"
    migrations_dir.mkdir(parents=True, exist_ok=True)

    # Copy the Mako template from the Skrift package
    skrift_dir = Path(__file__).parent
    src_mako = skrift_dir / "alembic" / "script.py.mako"
    dst_mako = project_root / "migrations" / "script.py.mako"

    if not dst_mako.exists() and src_mako.exists():
        shutil.copy2(src_mako, dst_mako)

    click.echo(f"Initialized migrations directory at {migrations_dir}")
    click.echo(f"Template at {dst_mako}")


def _run_alembic(project_root: Path, args: list[str]) -> None:
    """Build an Alembic Config programmatically and run the given command."""
    from alembic.config import Config, CommandLine

    skrift_dir = Path(__file__).parent

    # Find alembic.ini
    alembic_ini = project_root / "alembic.ini"
    if not alembic_ini.exists():
        alembic_ini = skrift_dir / "alembic.ini"
        if not alembic_ini.exists():
            click.echo("Error: Could not find alembic.ini", err=True)
            sys.exit(1)

    # Build version_locations: user dir first (if it exists), then Skrift's
    skrift_versions = str(skrift_dir / "alembic" / "versions")
    user_versions = project_root / "migrations" / "versions"
    if user_versions.is_dir():
        version_locations = f"{user_versions} {skrift_versions}"
    else:
        version_locations = skrift_versions

    # Rewrite "upgrade head" → "upgrade heads" when multiple locations exist.
    #
    # Known limitation (Alembic <=1.18): when user migrations use depends_on
    # to reference Skrift revision IDs across version locations, "upgrade heads"
    # can raise "RevisionError: Requested revision X overlaps with other
    # requested revisions Y" if the depended-on revision is already applied.
    # Workaround: run "skrift db upgrade <specific_revision>" for the new
    # location's head instead of relying on "upgrade heads".
    if user_versions.is_dir() and len(args) >= 2:
        if args[0] == "upgrade" and args[1] == "head":
            args = list(args)
            args[1] = "heads"
        elif args[0] == "downgrade" and args[1] == "base":
            args = list(args)
            args[1] = "base"

    # Build Config and inject version_locations
    cfg = Config(str(alembic_ini))
    cfg.set_main_option("version_locations", version_locations)

    # Parse and run through CommandLine for proper subcommand dispatch
    cmd = CommandLine()
    options = cmd.parser.parse_args(args)
    if not hasattr(options, "cmd"):
        cmd.parser.error("too few arguments")
    else:
        cfg.cmd_opts = options
        fn, positional, kwarg = options.cmd
        fn(
            cfg,
            *[getattr(options, k, None) for k in positional],
            **{k: getattr(options, k, None) for k in kwarg},
        )


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
        skrift db init             # Initialize user migrations directory
        skrift db upgrade heads    # Apply all migrations
        skrift db downgrade -1     # Rollback one migration
        skrift db current          # Show current revision
        skrift db history          # Show migration history
        skrift db revision -m "description" --autogenerate  # Create new migration
    """
    # Always run from the project root (where app.yaml and .env are)
    project_root = Path.cwd()
    if not (project_root / "app.yaml").exists():
        project_root = Path(__file__).parent.parent
    os.chdir(project_root)

    args = ctx.args

    # Intercept "init" as a custom subcommand
    if args and args[0] == "init":
        _db_init(project_root)
        return

    if not args:
        click.echo(ctx.get_help())
        return

    _run_alembic(project_root, args)


@cli.group()
def claude():
    """Manage Claude Code skills for Skrift development."""
    pass


cli.add_command(claude)


def _get_skill_names():
    """Discover skill subdirectories from the package."""
    import importlib.resources

    package_files = importlib.resources.files("skrift.claude_skill")
    names = []
    for item in package_files.iterdir():
        if item.is_dir() and item.joinpath("SKILL.md").is_file():
            names.append(item.name)
    names.sort()
    return names


def _find_installed_skills(skills_base: Path, skill_names: list[str]) -> list[str]:
    """Return skill names that already exist on disk."""
    return [n for n in skill_names if (skills_base / n).exists()]


def _install_skills(skills_base: Path, skill_names: list[str]) -> list[str]:
    """Copy skill files from the package to disk. Returns installed names."""
    import importlib.resources

    package_files = importlib.resources.files("skrift.claude_skill")
    installed = []
    for skill_name in skill_names:
        skill_dir = skills_base / skill_name
        skill_dir.mkdir(parents=True, exist_ok=True)

        source = package_files.joinpath(skill_name, "SKILL.md")
        dest = skill_dir / "SKILL.md"

        content = source.read_text()
        dest.write_text(content)
        click.echo(f"  {dest.relative_to(Path.cwd())}")
        installed.append(skill_name)
    return installed


def _remove_skills(skills_base: Path, skill_names: list[str]) -> list[str]:
    """Remove installed skill directories. Returns removed names."""
    import shutil

    removed = []
    for skill_name in skill_names:
        skill_dir = skills_base / skill_name
        if skill_dir.exists():
            shutil.rmtree(skill_dir)
            click.echo(f"  Removed {skill_dir.relative_to(Path.cwd())}")
            removed.append(skill_name)
    return removed


@claude.command()
def install():
    """Install Skrift skills for Claude Code.

    Fails if any Skrift skills are already installed.
    Use `skrift claude update` to replace existing skills.
    """
    skills_base = Path.cwd() / ".claude" / "skills"
    skill_names = _get_skill_names()

    if not skill_names:
        click.echo("Error: No skill directories found in package.", err=True)
        sys.exit(1)

    existing = _find_installed_skills(skills_base, skill_names)
    if existing:
        click.echo(f"Skrift skills already installed: {', '.join(existing)}", err=True)
        click.echo("Use `skrift claude update` to replace them.", err=True)
        sys.exit(1)

    installed = _install_skills(skills_base, skill_names)
    click.echo(f"\nInstalled {len(installed)} skills. Use /skrift to activate.")


@claude.command()
def remove():
    """Remove all installed Skrift skills."""
    skills_base = Path.cwd() / ".claude" / "skills"
    skill_names = _get_skill_names()

    existing = _find_installed_skills(skills_base, skill_names)
    if not existing:
        click.echo("No Skrift skills found to remove.")
        return

    removed = _remove_skills(skills_base, existing)
    click.echo(f"\nRemoved {len(removed)} skills.")


@claude.command()
def update():
    """Update Skrift skills to the latest version.

    Removes existing Skrift skills and installs fresh copies.
    """
    skills_base = Path.cwd() / ".claude" / "skills"
    skill_names = _get_skill_names()

    if not skill_names:
        click.echo("Error: No skill directories found in package.", err=True)
        sys.exit(1)

    existing = _find_installed_skills(skills_base, skill_names)
    if existing:
        _remove_skills(skills_base, existing)

    installed = _install_skills(skills_base, skill_names)
    click.echo(f"\nUpdated {len(installed)} skills. Use /skrift to activate.")


if __name__ == "__main__":
    cli()
