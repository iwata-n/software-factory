import os

from alembic import context
from sqlalchemy import create_engine

from software_factory.models import Base

target_metadata = Base.metadata
url = os.environ.get("FACTORY_DATABASE_URL")
if not url:
    raise RuntimeError("FACTORY_DATABASE_URL is required for migrations")

if context.is_offline_mode():
    context.configure(url=url, target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    engine = create_engine(url)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()
