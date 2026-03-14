"""
index.py — Main entry point for the Recharge Gateway.

Starts:
  1. Worker thread      (processes recharge queue)
  2. Health monitor     (checks modem every 60s)
  3. Flask API server   (HTTP endpoints)

Usage:
    python index.py

For testing individual parts, run each module standalone:
    python config.py    → verify configuration
    python modem.py     → test modem (AT, signal, CREG, SMS)
    python database.py  → inspect database & orders
    python worker.py    → worker info
    python api.py       → start API server only (no worker/monitor)
"""

import threading
import logging
import config
import modem
import database
import worker
from api import app


if __name__ == "__main__":

    logging.info("GATEWAY STARTED")

    # Initial setup: check modem + clean SMS + ensure network
    with config.serial_lock:
        modem.modem_check()
        modem.delete_all_sms()
        registered, stat = modem.check_registration()
        if not registered:
            logging.warning(f"STARTUP: not registered (CREG={stat}), forcing...")
            modem.force_register()

    # Start background threads
    threading.Thread(target=worker.worker, daemon=True).start()
    threading.Thread(target=modem.modem_health_monitor, daemon=True).start()

    # Start Flask server (foreground)
    app.run(host="0.0.0.0", port=5000)