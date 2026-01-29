"""CLI commands for Skrift database management."""

import sys
from pathlib import Path


def db() -> None:
    """Run Alembic database migrations.

    This is a thin wrapper around Alembic that sets up the correct working
    directory and passes through all arguments.

    Usage:
        skrift-db upgrade head     # Apply all migrations
        skrift-db downgrade -1     # Rollback one migration
        skrift-db current          # Show current revision
        skrift-db history          # Show migration history
        skrift-db revision -m "description" --autogenerate  # Create new migration
    """
    from alembic.config import main as alembic_main

    import os

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
            print("Error: Could not find alembic.ini", file=sys.stderr)
            print("Make sure you're running from the project root directory.", file=sys.stderr)
            sys.exit(1)

    # Build argv with config path at the beginning (before any subcommand)
    # Original argv: ['skrift-db', 'upgrade', 'head']
    # New argv: ['skrift-db', '-c', '/path/to/alembic.ini', 'upgrade', 'head']
    new_argv = [sys.argv[0], "-c", str(alembic_ini)] + sys.argv[1:]
    sys.argv = new_argv

    # Pass through all CLI arguments to Alembic
    sys.exit(alembic_main(sys.argv[1:]))


if __name__ == "__main__":
    db()
