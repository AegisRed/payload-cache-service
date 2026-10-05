import sqlite3
from unittest.mock import Mock

import pytest

from cache_service.database import enable_wal


def busy_error() -> sqlite3.OperationalError:
    error = sqlite3.OperationalError("database is locked")
    error.sqlite_errorcode = sqlite3.SQLITE_BUSY
    return error


def test_wal_initialization_retries_transient_contention() -> None:
    cursor = Mock()
    cursor.execute.side_effect = [busy_error(), None]
    enable_wal(cursor, timeout=1)
    assert cursor.execute.call_count == 2


def test_wal_initialization_stops_at_timeout() -> None:
    cursor = Mock()
    cursor.execute.side_effect = busy_error()
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        enable_wal(cursor, timeout=0.001)


def test_wal_initialization_does_not_retry_unrelated_errors() -> None:
    cursor = Mock()
    cursor.execute.side_effect = sqlite3.OperationalError("disk I/O error")
    with pytest.raises(sqlite3.OperationalError, match="disk I/O"):
        enable_wal(cursor, timeout=1)
    assert cursor.execute.call_count == 1
