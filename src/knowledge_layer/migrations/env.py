"""Alembic environment driven only by a verified operator CLI connection."""

from alembic import context

connection = context.config.attributes.get("connection")
metadata = context.config.attributes.get("target_metadata")
if connection is None or metadata is None:
    raise RuntimeError("Use python -m knowledge_layer.migrate for schema operations")

context.configure(
    connection=connection,
    target_metadata=metadata,
    compare_type=True,
    include_name=lambda name, type_, _parents: type_ != "table" or name in metadata.tables,
)
with context.begin_transaction():
    context.run_migrations()
