"""Every safety rule has at least one accept and one reject."""

import pytest

from app.db import get_schema
from app.safety import SafetyError, validate_sql

SCHEMA = get_schema()["mapping"]


def _sql(text: str) -> str:
    rewritten, _note = validate_sql(text, SCHEMA, max_rows=200)
    return rewritten


def test_accepts_common_reads():
    samples = [
        "SELECT name FROM products",
        "SELECT p.name, c.name AS category FROM products p "
        "JOIN categories c ON p.category_id = c.id",
        "SELECT status, COUNT(*) AS orders FROM orders GROUP BY status",
        "WITH recent AS (SELECT id, order_date FROM orders WHERE order_date >= '2026-10-01') "
        "SELECT COUNT(*) AS orders FROM recent",
        "SELECT region_id, COUNT(*) OVER (PARTITION BY region_id) AS n FROM customers",
        "SELECT name FROM products UNION SELECT name FROM categories",
        "SELECT name FROM products WHERE category_id IN (SELECT id FROM categories)",
        "SELECT strftime('%Y-%m', order_date) AS month, COUNT(*) AS orders "
        "FROM orders GROUP BY month",
        "SELECT p.name AS product_name FROM products AS p",
        "SELECT /* label */ name FROM products",
        "SELECT name FROM products LIMIT 10",
    ]
    for sample in samples:
        rewritten = _sql(sample)
        assert "SELECT" in rewritten.upper()
        assert "--" not in rewritten
        assert "/*" not in rewritten


def test_limit_rules():
    added, note = validate_sql("SELECT name FROM products", SCHEMA, max_rows=200)
    assert "LIMIT 200" in added
    assert "added" in note
    clamped, note = validate_sql("SELECT name FROM products LIMIT 5000", SCHEMA, max_rows=200)
    assert "LIMIT 200" in clamped
    assert "clamped" in note
    kept, _note = validate_sql("SELECT name FROM products LIMIT 10", SCHEMA, max_rows=200)
    assert "LIMIT 10" in kept
    assert "LIMIT 200" not in kept


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM orders",
        "DROP TABLE customers",
        "UPDATE products SET price = 1",
        "INSERT INTO categories (name) VALUES ('Hack')",
        "CREATE TABLE hack (id INT)",
        "ALTER TABLE products ADD COLUMN x TEXT",
        "PRAGMA table_info(orders)",
        "ATTACH DATABASE 'other.db' AS other",
        "SELECT 1; DROP TABLE x",
        "SELECT * FROM sqlite_master",
        "SELECT load_extension('x')",
        "SELECT readfile('/etc/passwd')",
        "SELECT name FROM missing_table",
        "SELECT not_a_column FROM products",
        "SELECT date('now')",
        "SELECT CURRENT_DATE",
        "SELECT 1 FROM orders; SELECT 2 FROM products",
        "SELECT 1 FROM orders; -- hide\nDROP TABLE customers",
        "WITH x AS (DELETE FROM orders RETURNING *) SELECT * FROM x",
        "",
        "   ",
    ],
)
def test_rejects(sql):
    with pytest.raises(SafetyError):
        validate_sql(sql, SCHEMA, max_rows=200)


def test_destructive_flag_skips_repair_class():
    blocked = [
        "DELETE FROM orders",
        "DROP TABLE customers",
        "UPDATE products SET price = 1",
        "SELECT 1; DROP TABLE x",
        "WITH x AS (DELETE FROM orders RETURNING *) SELECT * FROM x",
        "SELECT load_extension('x')",
    ]
    repairable = [
        "SELECT not_a_column FROM products",
        "SELECT date('now')",
        "SELECT CURRENT_DATE",
        "SELECT 1 FROM orders; SELECT 2 FROM products",
        "SELECT * FROM sqlite_master",
    ]
    for sql in blocked:
        with pytest.raises(SafetyError) as caught:
            validate_sql(sql, SCHEMA, max_rows=200)
        assert caught.value.destructive is True
    for sql in repairable:
        with pytest.raises(SafetyError) as caught:
            validate_sql(sql, SCHEMA, max_rows=200)
        assert caught.value.destructive is False


def test_date_error_mentions_reference_date():
    with pytest.raises(SafetyError) as caught:
        validate_sql("SELECT date('now', '-1 month')", SCHEMA, max_rows=200)
    assert "2026-11-19" in caught.value.reason
    assert "current time" in caught.value.reason


def test_rejects_overlong_sql():
    sql = "SELECT name FROM products WHERE name = '" + ("a" * 5000) + "'"
    with pytest.raises(SafetyError) as caught:
        validate_sql(sql, SCHEMA, max_rows=200)
    assert "4000" in caught.value.reason


def test_empty_message():
    with pytest.raises(SafetyError) as caught:
        validate_sql("  ", SCHEMA, max_rows=200)
    assert "empty" in caught.value.reason.lower()
