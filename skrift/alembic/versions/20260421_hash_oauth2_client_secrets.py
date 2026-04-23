"""Hash existing plaintext OAuth2 client secrets in place.

Upgrades ``oauth2_clients.client_secret`` from plaintext tokens to the
salted SHA-256 storage format used by
``skrift.auth.client_secret.hash_client_secret``. Rows that are already in
the hashed format (i.e. start with ``sha256$``) are left untouched, so the
upgrade is idempotent.

Downgrade is deliberately a no-op — we cannot recover plaintext from a
hash. An operator needing plaintext again must regenerate each client
secret through the admin UI after downgrading.

Revision ID: e5f6g7h8i9j0
Revises: d4e5f6a7b8c9
Create Date: 2026-04-21
"""

from alembic import op
import sqlalchemy as sa

from skrift.auth.client_secret import hash_client_secret, is_hashed

revision = "e5f6g7h8i9j0"
down_revision = "d4e5f6a7b8c9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    rows = connection.execute(
        sa.text(
            "SELECT id, client_secret FROM oauth2_clients "
            "WHERE client_secret IS NOT NULL AND client_secret != ''"
        )
    ).fetchall()
    for row in rows:
        if is_hashed(row.client_secret):
            continue
        hashed = hash_client_secret(row.client_secret)
        connection.execute(
            sa.text(
                "UPDATE oauth2_clients SET client_secret = :secret WHERE id = :id"
            ),
            {"secret": hashed, "id": row.id},
        )


def downgrade() -> None:
    # Plaintext is unrecoverable from a hash. An operator downgrading past
    # this revision must regenerate every client secret via the admin UI.
    pass
