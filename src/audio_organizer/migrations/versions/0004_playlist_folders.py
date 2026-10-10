"""Where playlist files are written: a folder and formats per library and per sync target.

Revision ID: 0004
Revises: 0003
"""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table, formats in (("library", "m3u"), ("sync_target", "m3u,json,html")):
        with op.batch_alter_table(table) as batch:
            batch.add_column(
                sa.Column("playlist_folder", sa.Text(), server_default="", nullable=False)
            )
            batch.add_column(
                sa.Column("playlist_formats", sa.String(50), server_default=formats, nullable=False)
            )


def downgrade() -> None:
    for table in ("library", "sync_target"):
        with op.batch_alter_table(table) as batch:
            batch.drop_column("playlist_formats")
            batch.drop_column("playlist_folder")
