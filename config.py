"""
config.py — Shared configuration and global state for the gateway.

Run standalone to verify config:
    python config.py
"""

import os
import threading
import queue
import logging

# =====================
# CONFIG
# =====================

API_TOKEN        = os.environ.get("GATEWAY_TOKEN", "123456SECRET")
API_CALLBACK_URL = os.environ.get("API_CALLBACK_URL", "https://credit.o-dev.store/api/gateway/callback")
SERIAL_PORT      = "/dev/ttyAMA0"
BAUDRATE         = 9600
MIN_SIGNAL       = 5           # Minimum CSQ signal level (0-31)
DB_PATH          = "database.db"

# =====================
# SHARED STATE
# =====================

MODEM_OK              = True   # Global modem health flag (updated by modem_health_monitor)
SIM_BALANCE           = None   # Last known SIM balance in MAD (updated by check_balance)
RECHARGE_IN_PROGRESS  = False  # True while a recharge is executing (blocks health monitor)
serial_lock = threading.Lock()
task_queue  = queue.Queue()

# =====================
# LOGGING
# =====================

logging.basicConfig(
    filename="recharge.log",
    level=logging.INFO,
    format="%(asctime)s %(message)s"
)
# Also log to console so we see output when running standalone
console = logging.StreamHandler()
console.setLevel(logging.INFO)
console.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
logging.getLogger().addHandler(console)


# =====================
# SELF-TEST
# =====================

if __name__ == "__main__":
    print("=" * 40)
    print("  Gateway Config")
    print("=" * 40)
    print(f"  API_TOKEN      : {API_TOKEN[:4]}***")
    print(f"  CALLBACK_URL   : {API_CALLBACK_URL}")
    print(f"  SERIAL_PORT    : {SERIAL_PORT}")
    print(f"  BAUDRATE    : {BAUDRATE}")
    print(f"  MIN_SIGNAL  : {MIN_SIGNAL}")
    print(f"  DB_PATH     : {DB_PATH}")
    print(f"  MODEM_OK    : {MODEM_OK}")
    print(f"  Queue size  : {task_queue.qsize()}")
    print("=" * 40)
    print("  Config OK ✓")
