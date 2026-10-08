"""Alembic environment. Migrations run from `db.migrate`, which hands over its connection."""

from alembic import context

from audio_organizer.models import Base

connection = context.config.attributes["connection"]
context.configure(
    connection=connection,
    target_metadata=Base.metadata,
    render_as_batch=True,  # SQLite cannot alter most things in place
)
with context.begin_transaction():
    context.run_migrations()
