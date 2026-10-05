import sqlite3
from pathlib import Path

from sqlalchemy import URL, Connection, Engine, create_engine, event

from cache_service.models import Base


def create_database(data_dir: Path, timeout: float) -> Engine:
    data_dir.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        URL.create("sqlite", database=str(data_dir / "cache.sqlite3")),
        connect_args={"check_same_thread": False, "timeout": timeout},
    )

    @event.listens_for(engine, "connect")
    def configure_connection(connection: sqlite3.Connection, _record: object) -> None:
        # Explicit transactions avoid sqlite3's legacy implicit-BEGIN behavior.
        connection.isolation_level = None
        cursor = connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()

    @event.listens_for(engine, "begin")
    def begin_transaction(connection: Connection) -> None:
        # Creators reserve the writer lock before inspecting cache misses. Readers
        # use deferred transactions and can continue against the WAL snapshot.
        mode = "IMMEDIATE" if connection.get_execution_options().get("cache_writer") else "DEFERRED"
        connection.exec_driver_sql(f"BEGIN {mode}")

    try:
        # Coordinate first startup too: workers must not race to create tables.
        with engine.execution_options(cache_writer=True).begin() as connection:
            Base.metadata.create_all(connection)
    except Exception:
        engine.dispose()
        raise
    return engine
