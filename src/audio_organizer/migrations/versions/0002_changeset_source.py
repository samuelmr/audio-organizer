"""Record which feature proposed each changeset.

Revision ID: 0002
Revises: 0001
"""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("changeset") as batch:
        batch.add_column(sa.Column("source", sa.String(30), nullable=True))
        batch.create_index("ix_changeset_source", ["source"])


def downgrade() -> None:
    with op.batch_alter_table("changeset") as batch:
        batch.drop_index("ix_changeset_source")
        batch.drop_column("source")
