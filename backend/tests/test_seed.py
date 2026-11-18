"""The seed is deterministic and obeys the store's date and money rules."""

import importlib.util
import sqlite3
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "seed_database.py"
EXPECTED_HASH = "d4f747071d39868c06ddc0ec55fae766f33b1217f8b0b283f9afc55404fc11eb"
EXPECTED_COUNTS = {
    "regions": 8,
    "customers": 450,
    "categories": 6,
    "products": 33,
    "orders": 3600,
    "order_items": 5780,
    "reviews": 1013,
}


def _build(out: Path) -> dict:
    spec = importlib.util.spec_from_file_location("seed_database", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.build(out)


def test_two_runs_match_and_counts(tmp_path):
    first = _build(tmp_path / "a" / "store.db")
    second = _build(tmp_path / "b" / "store.db")
    assert first["content_sha256"] == second["content_sha256"] == EXPECTED_HASH
    assert first["row_counts"] == EXPECTED_COUNTS


def test_dates_totals_and_injected_reviews(tmp_path):
    out = tmp_path / "store.db"
    _build(out)
    conn = sqlite3.connect(f"file:{out}?mode=ro", uri=True)
    early_orders = conn.execute(
        """
        SELECT COUNT(*) FROM orders o
        JOIN customers c ON c.id = o.customer_id
        WHERE o.order_date < c.signup_date
        """
    ).fetchone()[0]
    early_items = conn.execute(
        """
        SELECT COUNT(*) FROM order_items oi
        JOIN orders o ON o.id = oi.order_id
        JOIN products p ON p.id = oi.product_id
        WHERE o.order_date < p.launch_date
        """
    ).fetchone()[0]
    bad_totals = conn.execute(
        """
        SELECT COUNT(*) FROM orders o
        LEFT JOIN (
            SELECT order_id, SUM(quantity * unit_price) AS total
            FROM order_items GROUP BY order_id
        ) items ON items.order_id = o.id
        WHERE o.total_amount != IFNULL(items.total, -1)
        """
    ).fetchone()[0]
    injected = conn.execute(
        """
        SELECT COUNT(*) FROM reviews
        WHERE comment LIKE '%ignore all previous instructions%'
           OR comment LIKE '%DROP TABLE customers%'
        """
    ).fetchone()[0]
    conn.close()
    assert early_orders == 0
    assert early_items == 0
    assert bad_totals == 0
    assert injected == 2
