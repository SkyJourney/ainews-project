import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# DATABASE_URL 从环境变量读取，不在 alembic.ini 里硬编码（见该文件顶部说明）。
database_url = os.environ.get(
    "DATABASE_URL", "postgresql+psycopg://ainews:ainews@localhost:15432/ainews_content"
)
config.set_main_option("sqlalchemy.url", database_url)

# 03-architecture-proposal.md §3 的表结构在 migration 里用 op.execute() 原样写 DDL
# （GENERATED ALWAYS AS STORED / GIN 索引等 Postgres 特性），不引入 SQLAlchemy ORM
# models 做 autogenerate——target_metadata 保持 None。
target_metadata = None


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
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
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
