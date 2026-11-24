"""轻量幂等迁移：create_all 只补新表，已存在表的新增列需要显式 ALTER。
应用启动时执行，重复执行无副作用。"""
from sqlalchemy import inspect, text

from src.config.database import engine

_COLUMN_MIGRATIONS = {
    'runs': {
        'idem_key': "VARCHAR(120)",
        'publication_id': 'INTEGER',
    },
}


def _pg_constraints(conn, table):
    return {row[0] for row in conn.execute(text(
        "SELECT conname FROM pg_constraint WHERE conrelid = :reg::regclass"
    ), {'reg': table})}


def run_migrations():
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    is_postgres = engine.dialect.name == 'postgresql'
    with engine.begin() as conn:
        for table, columns in _COLUMN_MIGRATIONS.items():
            if table not in existing_tables:
                continue
            present = {column['name'] for column in inspector.get_columns(table)}
            for name, ddl_type in columns.items():
                if name in present:
                    continue
                conn.execute(text(f'ALTER TABLE {table} ADD COLUMN {name} {ddl_type}'))
        if 'runs' in existing_tables:
            indexes = {index['name'] for index in inspector.get_indexes('runs')}
            if 'ix_runs_idem_key' not in indexes:
                conn.execute(text('CREATE INDEX IF NOT EXISTS ix_runs_idem_key ON runs (idem_key)'))
            if is_postgres:
                constraints = _pg_constraints(conn, 'runs')
                if 'uq_runs_idem_key' not in constraints:
                    # 历史空值不冲突；SQL 标准唯一约束允许多个 NULL。
                    conn.execute(text('ALTER TABLE runs ADD CONSTRAINT uq_runs_idem_key UNIQUE (idem_key)'))
        if is_postgres and 'report_publications' in existing_tables:
            constraints = _pg_constraints(conn, 'report_publications')
            for name, ddl in (
                ('uq_publication_day_version',
                 'ALTER TABLE report_publications ADD CONSTRAINT uq_publication_day_version '
                 'UNIQUE (report_day, version)'),
                ('uq_publication_run',
                 'ALTER TABLE report_publications ADD CONSTRAINT uq_publication_run UNIQUE (run_id)'),
            ):
                if name not in constraints:
                    conn.execute(text(ddl))
