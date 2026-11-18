"""The connection refuses writes even when the SQL checker is bypassed."""

import shutil
import sqlite3

import pytest

from app.db import QueryTimeout, execute_select, open_connection, resolved_db_path


def test_mode_ro_rejects_writes():
    conn = open_connection(read_only_uri=True, query_only=False, authorizer=False)
    try:
        with pytest.raises(sqlite3.Error):
            conn.execute("UPDATE products SET price = 1")
    finally:
        conn.close()


def test_query_only_rejects_writes(tmp_path):
    copy = tmp_path / "store.db"
    shutil.copy(resolved_db_path(), copy)
    conn = open_connection(copy, read_only_uri=False, query_only=True, authorizer=False)
    try:
        with pytest.raises(sqlite3.Error):
            conn.execute("DELETE FROM orders")
    finally:
        conn.close()


def test_authorizer_rejects_writes(tmp_path):
    copy = tmp_path / "store.db"
    shutil.copy(resolved_db_path(), copy)
    conn = open_connection(copy, read_only_uri=False, query_only=False, authorizer=True)
    try:
        with pytest.raises(sqlite3.Error):
            conn.execute("INSERT INTO categories (name) VALUES ('Hack')")
    finally:
        conn.close()


def test_timeout():
    sql = (
        "WITH RECURSIVE c(n) AS ("
        "SELECT 1 UNION ALL SELECT n + 1 FROM c WHERE n < 100000000"
        ") SELECT MAX(n) FROM c"
    )
    with pytest.raises(QueryTimeout):
        execute_select(sql, timeout_s=0.4, max_rows=10)


def test_row_cap_sets_truncated():
    sql = (
        "WITH RECURSIVE c(n) AS ("
        "SELECT 1 UNION ALL SELECT n + 1 FROM c WHERE n < 250"
        ") SELECT n FROM c"
    )
    result = execute_select(sql, max_rows=200, timeout_s=5)
    assert result["truncated"] is True
    assert len(result["rows"]) == 200
