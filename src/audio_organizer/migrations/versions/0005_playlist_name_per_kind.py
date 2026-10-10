"""A smart and a static playlist may share a name: unique per library, name and kind.

Revision ID: 0005
Revises: 0004
"""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

# The old constraint was never named; this convention gives the reflected one a name that
# can be dropped
NAMING = {"uq": "uq_%(table_name)s_%(column_0_N_name)s"}


def upgrade() -> None:
    with op.batch_alter_table("playlist", naming_convention=NAMING) as batch:
        batch.drop_constraint("uq_playlist_library_id_name", type_="unique")
    op.create_index(
        "uq_playlist_library_name_kind",
        "playlist",
        ["library_id", "name", sa.text("(rule IS NULL)")],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_playlist_library_name_kind", table_name="playlist")
    with op.batch_alter_table("playlist") as batch:
        batch.create_unique_constraint("uq_playlist_library_id_name", ["library_id", "name"])
