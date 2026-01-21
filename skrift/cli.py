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

    # Ensure we're running from the project root where alembic.ini is located
    # If alembic.ini is not in cwd, check common locations
    alembic_ini = Path.cwd() / "alembic.ini"

    if not alembic_ini.exists():
        # Try to find alembic.ini relative to this module
        module_dir = Path(__file__).parent.parent
        alembic_ini = module_dir / "alembic.ini"

        if alembic_ini.exists():
            # Change to the directory containing alembic.ini
            import os
            os.chdir(module_dir)
        else:
            print("Error: Could not find alembic.ini", file=sys.stderr)
            print("Make sure you're running from the project root directory.", file=sys.stderr)
            sys.exit(1)

    # Pass through all CLI arguments to Alembic
    sys.exit(alembic_main(sys.argv[1:]))


if __name__ == "__main__":
    db()
