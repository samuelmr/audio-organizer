"""Let a change have no audio file (trashing leftover files that are not in the catalog).

Revision ID: 0003
Revises: 0002
"""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("change") as batch:
        batch.alter_column("file_id", existing_type=sa.Integer(), nullable=True)


def downgrade() -> None:
    with op.batch_alter_table("change") as batch:
        batch.alter_column("file_id", existing_type=sa.Integer(), nullable=False)
