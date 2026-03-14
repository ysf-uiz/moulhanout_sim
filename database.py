"""
database.py — SQLite operations for the gateway.

Thread-safe: each function opens its own connection.
SQLite with WAL mode allows concurrent reads + sequential writes.

Run standalone to test database CRUD:
    python database.py
"""

import sqlite3
import threading
import logging
import config


# =====================
# INIT
# =====================

_db_lock = threading.Lock()


def _get_conn():
    """Create a new SQLite connection for the calling thread."""
    conn = sqlite3.connect(config.DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db():
    """Create the orders table if it doesn't exist."""
    conn = _get_conn()
    try:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS orders(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id TEXT UNIQUE,
            phone TEXT,
            price TEXT,
            offer TEXT,
            status TEXT,
            date TEXT
        )
        """)
        conn.commit()
    finally:
        conn.close()


# Auto-init on import
init_db()


# =====================
# OPERATIONS
# =====================

def order_exists(order_id):
    """Check if an order already exists (anti-duplicate)."""
    conn = _get_conn()
    try:
        cur = conn.execute("SELECT 1 FROM orders WHERE order_id=?", (order_id,))
        return cur.fetchone() is not None
    finally:
        conn.close()


def insert_order(order_id, phone, price, offer, status='queued'):
    """Insert a new order with initial status."""
    with _db_lock:
        conn = _get_conn()
        try:
            conn.execute("""
            INSERT INTO orders(order_id, phone, price, offer, status, date)
            VALUES(?, ?, ?, ?, ?, datetime('now'))
            """, (order_id, phone, price, offer, status))
            conn.commit()
        finally:
            conn.close()


def update_order_status(order_id, status):
    """Update the status of an existing order."""
    with _db_lock:
        conn = _get_conn()
        try:
            conn.execute("""
            UPDATE orders SET status=?, date=datetime('now')
            WHERE order_id=?
            """, (status, order_id))
            conn.commit()
        finally:
            conn.close()


def get_order_status(order_id):
    """Get the status of an order. Returns None if not found."""
    conn = _get_conn()
    try:
        cur = conn.execute("SELECT status FROM orders WHERE order_id=?", (order_id,))
        r = cur.fetchone()
        return r[0] if r else None
    finally:
        conn.close()


def count_orders(status=None):
    """Count orders, optionally filtered by status."""
    conn = _get_conn()
    try:
        if status:
            cur = conn.execute("SELECT COUNT(*) FROM orders WHERE status=?", (status,))
        else:
            cur = conn.execute("SELECT COUNT(*) FROM orders")
        return cur.fetchone()[0]
    finally:
        conn.close()


def get_recent_orders(limit=20):
    """Get the most recent orders."""
    conn = _get_conn()
    try:
        cur = conn.execute("""
        SELECT order_id, phone, price, offer, status, date
        FROM orders ORDER BY id DESC LIMIT ?
        """, (limit,))
        return cur.fetchall()
    finally:
        conn.close()


# =====================
# SELF-TEST
# =====================

if __name__ == "__main__":
    print("=" * 40)
    print("  Database Self-Test")
    print("=" * 40)

    # Stats
    total   = count_orders()
    success = count_orders('success')
    failed  = count_orders('failed')
    rejected = count_orders('rejected')
    queued  = count_orders('queued')
    pending = count_orders('processing')

    print(f"  Total orders   : {total}")
    print(f"  Success        : {success}")
    print(f"  Failed         : {failed}")
    print(f"  Rejected       : {rejected}")
    print(f"  Queued         : {queued}")
    print(f"  Processing     : {pending}")

    # Recent orders
    recent = get_recent_orders(5)
    if recent:
        print(f"\n  Last {len(recent)} orders:")
        print(f"  {'Order ID':<20} {'Phone':<15} {'Price':<8} {'Status':<12} {'Date'}")
        print(f"  {'-'*75}")
        for row in recent:
            oid, phone, price, offer, status, date = row
            print(f"  {oid:<20} {phone:<15} {price:<8} {status:<12} {date}")
    else:
        print("\n  No orders yet.")

    print("=" * 40)
    print("  Database OK ✓")
