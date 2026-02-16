"""Verify Alembic migration chain integrity."""

import importlib.util
from pathlib import Path


VERSIONS_DIR = Path(__file__).resolve().parent.parent / "skrift" / "alembic" / "versions"


def _load_migrations():
    """Load all migration modules and return list of (filename, revision, down_revision)."""
    migrations = []
    for path in sorted(VERSIONS_DIR.glob("*.py")):
        if path.name == "__init__.py":
            continue
        spec = importlib.util.spec_from_file_location(path.stem, path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        migrations.append((path.name, mod.revision, mod.down_revision))
    return migrations


def test_no_duplicate_revision_ids():
    migrations = _load_migrations()
    revisions = [rev for _, rev, _ in migrations]
    duplicates = [r for r in revisions if revisions.count(r) > 1]
    assert not duplicates, f"Duplicate revision IDs: {set(duplicates)}"


def test_single_head_linear_chain():
    migrations = _load_migrations()
    down_revs = {rev: down for _, rev, down in migrations}

    # Find heads: revisions that no other revision points to as down_revision
    all_revs = set(down_revs.keys())
    referenced = {d for d in down_revs.values() if d is not None}
    heads = all_revs - referenced

    # Alembic allows multiple heads but we want a single linear chain
    assert len(heads) == 1, f"Expected 1 head, found {len(heads)}: {heads}"

    # Find roots (down_revision is None)
    roots = [rev for rev, down in down_revs.items() if down is None]
    assert len(roots) == 1, f"Expected 1 root, found {len(roots)}: {roots}"
