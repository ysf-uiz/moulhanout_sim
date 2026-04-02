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
ADMIN_TOKEN      =  API_TOKEN #os.environ.get("GATEWAY_ADMIN_TOKEN", "0650421408").strip()
API_CALLBACK_URL = os.environ.get("API_CALLBACK_URL", "https://credit.o-dev.store/api/gateway/callback")
MIN_SIGNAL       = 5           # Minimum CSQ signal level (0-31)
DB_PATH          = "database.db"
MAX_QUEUE_WAIT_SEC = int(os.environ.get("MAX_QUEUE_WAIT_SEC", "300"))
HEALTH_CHECK_INTERVAL_SEC = int(os.environ.get("HEALTH_CHECK_INTERVAL_SEC", "60"))
MODEM_OFFLINE_ALERT_SEC = int(os.environ.get("MODEM_OFFLINE_ALERT_SEC", "300"))
MODEM_OFFLINE_ALERT_RETRY_SEC = int(os.environ.get("MODEM_OFFLINE_ALERT_RETRY_SEC", "300"))
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

# =====================
# PER-MODEM CONFIG
# =====================

MODEMS = {
    "orange": {
        "serial_port":  "/dev/ttyAMA0",
        "baudrate":     9600,
        "modem_ok":     True,
        "sim_balance":  None,
        "last_signal":  -1,
        "last_registered": False,
        "last_creg_stat": -1,
        "last_health_check_ts": 0.0,
        "recharge_in_progress": False,
        "serial_lock":  threading.Lock(),
        "task_queue":   queue.Queue(),
        "recharge_code_template": "1391997{phone}{price}*{offer}",
        "balance_ussd": "#555*4*2#",
    },
    "inwi": {
        "serial_port":  "/dev/ttyAMA4",
        "baudrate":     9600,
        "modem_ok":     True,
        "sim_balance":  None,
        "last_signal":  -1,
        "last_registered": False,
        "last_creg_stat": -1,
        "last_health_check_ts": 0.0,
        "recharge_in_progress": False,
        "serial_lock":  threading.Lock(),
        "task_queue":   queue.Queue(),
        "recharge_code_template": "*139*{phone}*{price}*{offer}#",
        "balance_ussd": "*139*5#",
    },
}

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
    print(f"  ADMIN_TOKEN    : {'SET' if ADMIN_TOKEN else 'NOT_SET'}")
    print(f"  CALLBACK_URL   : {API_CALLBACK_URL}")
    print(f"  MIN_SIGNAL     : {MIN_SIGNAL}")
    print(f"  DB_PATH        : {DB_PATH}")
    print(f"  MAX_QUEUE_WAIT : {MAX_QUEUE_WAIT_SEC}s")
    print(f"  HEALTH_EVERY   : {HEALTH_CHECK_INTERVAL_SEC}s")
    print(f"  OFFLINE_ALERT  : {MODEM_OFFLINE_ALERT_SEC}s")
    print(f"  TELEGRAM_READY : {'YES' if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID else 'NO'}")
    for carrier, cfg in MODEMS.items():
        print(f"\n  [{carrier.upper()}]")
        print(f"    SERIAL_PORT  : {cfg['serial_port']}")
        print(f"    BAUDRATE     : {cfg['baudrate']}")
        print(f"    BALANCE_USSD : {cfg['balance_ussd']}")
        print(f"    RECHARGE_TPL : {cfg['recharge_code_template']}")
        print(f"    Queue size   : {cfg['task_queue'].qsize()}")
    print("=" * 40)
    print("  Config OK")
