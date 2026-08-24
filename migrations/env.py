from __future__ import annotations

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.config import settings
from app.extensions import Base
from app import models  # noqa: F401  (register every table on Base.metadata)

config = context.config
config.set_main_option("sqlalchemy.url", settings.database_url)
target_metadata = Base.metadata


# The postgis image also installs postgis_topology and postgis_tiger_geocoder, whose schemas sit
# on the search_path. Alembic must never propose dropping anything it did not create, so
# reflected objects are only managed when they belong to a table in our own metadata.
EXTENSION_SCHEMAS = {"tiger", "tiger_data", "topology"}


def include_object(obj, name, type_, reflected, compare_to):
    schema = getattr(obj, "schema", None)
    if schema in EXTENSION_SCHEMAS:
        return False
    if type_ == "table":
        if name in ("spatial_ref_sys", "geography_columns", "geometry_columns"):
            return False
        if reflected and name not in target_metadata.tables:
            return False
    if type_ in ("index", "unique_constraint", "foreign_key_constraint", "column"):
        table = getattr(obj, "table", None)
        table_name = getattr(table, "name", None)
        if reflected and table_name and table_name not in target_metadata.tables:
            return False
    return True


def run_migrations_offline() -> None:
    context.configure(
        url=settings.database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        include_object=include_object,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_object=include_object,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
