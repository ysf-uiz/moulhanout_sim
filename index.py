"""
index.py — Main entry point for the Recharge Gateway.

Starts:
  1. Worker threads     (one per modem, processes recharge queue)
  2. Health monitors    (one per modem, checks health every 60s)
  3. Flask API server   (HTTP endpoints)

Usage:
    python index.py

For testing individual parts, run each module standalone:
    python config.py    -> verify configuration
    python modem.py     -> test IAM modem (default)
    python modem.py inwi -> test Inwi modem
    python database.py  -> inspect database & orders
    python worker.py    -> worker info
    python api.py       -> start API server only (no worker/monitor)
"""

import threading
import logging
import config
from modem import Modem
import database
import worker
import api
from api import app


if __name__ == "__main__":

    logging.info("GATEWAY STARTED")

    # Create and initialize Modem instances
    modems = {}
    for carrier, cfg in config.MODEMS.items():
        logging.info(f"[{carrier}] Initializing modem on {cfg['serial_port']}...")
        try:
            m = Modem(carrier, cfg)

            # Initial setup: check modem + clean SMS + ensure network
            with cfg["serial_lock"]:
                m.modem_check()
                m.delete_all_sms()
                registered, stat = m.check_registration()
                if not registered:
                    logging.warning(f"[{carrier}] STARTUP: not registered (CREG={stat}), forcing...")
                    m.force_register()

            modems[carrier] = m
            logging.info(f"[{carrier}] Modem initialized OK")
        except Exception as e:
            logging.error(f"[{carrier}] FAILED to initialize modem on {cfg['serial_port']}: {e}")
            cfg["modem_ok"] = False

    # Make modem instances accessible to api.py
    api.modem_instances = modems

    # Start per-modem worker and health threads
    for carrier, m in modems.items():
        threading.Thread(
            target=worker.worker, args=(m,), daemon=True, name=f"worker-{carrier}"
        ).start()
        threading.Thread(
            target=m.modem_health_monitor, daemon=True, name=f"health-{carrier}"
        ).start()
        logging.info(f"[{carrier}] Worker and health monitor threads started")

    # Start Flask server (foreground)
    logging.info(f"Starting API server on port 5000 with {len(modems)} modem(s): {list(modems.keys())}")
    app.run(host="0.0.0.0", port=5000)
