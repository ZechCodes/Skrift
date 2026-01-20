#!/usr/bin/env python3
"""
Migration script to add user_id column to pages table.

This script adds the user_id foreign key column to the existing pages table.
Run this once after updating to the new schema.

Usage:
    python migrate_add_user_id.py
"""

import asyncio
import sqlite3
from pathlib import Path


async def migrate():
    """Add user_id column to pages table."""
    db_path = Path(__file__).parent / "app.db"

    if not db_path.exists():
        print(f"Database not found at {db_path}")
        print("Nothing to migrate. The schema will be created when the app first runs.")
        return

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    try:
        # Check if pages table exists
        cursor.execute("""
            SELECT name FROM sqlite_master
            WHERE type='table' AND name='pages'
        """)
        if not cursor.fetchone():
            print("Pages table does not exist yet.")
            print("Nothing to migrate. The schema will be created when the app first runs.")
            return

        # Check if user_id column already exists
        cursor.execute("PRAGMA table_info(pages)")
        columns = [row[1] for row in cursor.fetchall()]

        if "user_id" in columns:
            print("✓ Migration already applied: user_id column exists")
            return

        print("Adding user_id column to pages table...")

        # Add the user_id column (nullable, with index)
        cursor.execute("""
            ALTER TABLE pages
            ADD COLUMN user_id TEXT
        """)

        # Create index on user_id
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS ix_pages_user_id
            ON pages (user_id)
        """)

        conn.commit()
        print("✓ Migration complete: user_id column added successfully")

    except sqlite3.Error as e:
        conn.rollback()
        print(f"✗ Migration failed: {e}")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    asyncio.run(migrate())
