import sqlite3
import time
from pathlib import Path

from sqlalchemy import URL, Connection, Engine, create_engine, event

from cache_service.models import Base


def enable_wal(cursor: sqlite3.Cursor, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            return
        except sqlite3.OperationalError as exc:
            remaining = deadline - time.monotonic()
            code = getattr(exc, "sqlite_errorcode", 0) & 0xFF
            if code not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED) or remaining <= 0:
                raise
            # Concurrent first connections can race to switch journal mode. That
            # lock upgrade may bypass SQLite's busy timeout, so retry explicitly.
            time.sleep(min(0.01, remaining))


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
            enable_wal(cursor, timeout)
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
