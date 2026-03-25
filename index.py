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
import time
from datetime import datetime, timezone
import config
from modem import Modem
import database
import worker
import api
from api import app


def recover_pending_orders(modems):
    """Re-enqueue pending orders after process restart.

    Queue data lives in memory, so queued/processing rows can be left behind
    in SQLite if the process restarts. This function restores them.
    """
    pending = database.get_pending_orders(limit=1000)
    if not pending:
        return

    restored = 0
    skipped = 0

    for row in pending:
        order_id, phone, price, offer, carrier, status, _date = row
        carrier = (carrier or "").lower().strip()

        queued_at_ts = time.time()
        if _date:
            try:
                dt = datetime.strptime(_date, "%Y-%m-%d %H:%M:%S")
                queued_at_ts = dt.replace(tzinfo=timezone.utc).timestamp()
            except Exception:
                pass

        if carrier not in config.MODEMS:
            skipped += 1
            logging.error(f"STARTUP RECOVERY: order {order_id} has unknown carrier '{carrier}', skipped")
            continue

        if carrier not in modems:
            skipped += 1
            logging.error(f"STARTUP RECOVERY: modem '{carrier}' unavailable, order {order_id} kept pending")
            continue

        if status == "processing":
            database.update_order_status(order_id, "queued")

        task = {
            "order_id": order_id,
            "phone": phone,
            "price": price,
            "offer": offer,
            "carrier": carrier,
            "queued_at": queued_at_ts,
        }
        config.MODEMS[carrier]["task_queue"].put(task)
        restored += 1

    logging.warning(f"STARTUP RECOVERY: restored={restored}, skipped={skipped}")


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
                alive = m.modem_check()
                cfg["modem_ok"] = alive
                m.delete_all_sms()
                signal = -1
                registered = False
                stat = -1
                if alive:
                    signal = m.get_signal()
                    registered, stat = m.check_registration()
                if alive and not registered:
                    logging.warning(f"[{carrier}] STARTUP: not registered (CREG={stat}), forcing...")
                    m.force_register()
                    signal = m.get_signal()
                    registered, stat = m.check_registration()

                cfg["last_signal"] = signal
                cfg["last_registered"] = registered
                cfg["last_creg_stat"] = stat
                cfg["last_health_check_ts"] = time.time()

            modems[carrier] = m
            logging.info(f"[{carrier}] Modem initialized OK")
        except Exception as e:
            logging.error(f"[{carrier}] FAILED to initialize modem on {cfg['serial_port']}: {e}")
            cfg["modem_ok"] = False

    # Make modem instances accessible to api.py
    api.modem_instances = modems

    # Rebuild in-memory queues from DB state after restart
    recover_pending_orders(modems)

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
