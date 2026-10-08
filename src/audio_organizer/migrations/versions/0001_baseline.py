"""Baseline: the schema as it was when migrations were introduced.

Catalogs from before Alembic are stamped with this revision; it does nothing itself.
Tables are still created by create_all, so later revisions only change existing tables.

Revision ID: 0001
Revises:
"""

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
