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
            carrier TEXT,
            status TEXT,
            date TEXT
        )
        """)
        conn.commit()
    finally:
        conn.close()


def _migrate_add_carrier():
    """Add carrier column to existing orders table if missing."""
    conn = _get_conn()
    try:
        cur = conn.execute("PRAGMA table_info(orders)")
        columns = [row[1] for row in cur.fetchall()]
        if "carrier" not in columns:
            conn.execute("ALTER TABLE orders ADD COLUMN carrier TEXT DEFAULT 'orange'")
            conn.commit()
            logging.info("DB MIGRATION: added 'carrier' column (default='orange')")
    finally:
        conn.close()


# Auto-init on import
init_db()
_migrate_add_carrier()


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


def insert_order(order_id, phone, price, offer, status='queued', carrier=None):
    """Insert a new order with initial status."""
    with _db_lock:
        conn = _get_conn()
        try:
            conn.execute("""
            INSERT INTO orders(order_id, phone, price, offer, carrier, status, date)
            VALUES(?, ?, ?, ?, ?, ?, datetime('now'))
            """, (order_id, phone, price, offer, carrier, status))
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


def count_orders(status=None, carrier=None):
    """Count orders, optionally filtered by status and/or carrier."""
    conn = _get_conn()
    try:
        query = "SELECT COUNT(*) FROM orders"
        params = []
        conditions = []
        if status:
            conditions.append("status=?")
            params.append(status)
        if carrier:
            conditions.append("carrier=?")
            params.append(carrier)
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        cur = conn.execute(query, params)
        return cur.fetchone()[0]
    finally:
        conn.close()


def get_recent_orders(limit=20, carrier=None):
    """Get the most recent orders, optionally filtered by carrier."""
    conn = _get_conn()
    try:
        if carrier:
            cur = conn.execute("""
            SELECT order_id, phone, price, offer, carrier, status, date
            FROM orders WHERE carrier=? ORDER BY id DESC LIMIT ?
            """, (carrier, limit))
        else:
            cur = conn.execute("""
            SELECT order_id, phone, price, offer, carrier, status, date
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
        print(f"  {'Order ID':<20} {'Phone':<15} {'Price':<8} {'Carrier':<10} {'Status':<12} {'Date'}")
        print(f"  {'-'*85}")
        for row in recent:
            oid, phone, price, offer, carrier, status, date = row
            print(f"  {oid:<20} {phone:<15} {price:<8} {carrier or 'N/A':<10} {status:<12} {date}")
    else:
        print("\n  No orders yet.")

    print("=" * 40)
    print("  Database OK ✓")
