"""
worker.py — Queue consumer that processes recharge tasks ONE AT A TIME.

The worker is the ONLY component that executes recharges.
It holds the modem lock for the entire recharge lifecycle:
  lock → send USSD → wait SMS → log → cleanup → unlock → next

Run standalone to process one test task (without the API server):
    python worker.py
"""

import time
import logging
import requests
import config
import modem
import database


# =====================
# CALLBACK TO LARAVEL
# =====================

MAX_CALLBACK_RETRIES = 5
CALLBACK_RETRY_DELAYS = [2, 5, 10, 30, 60]  # seconds between retries


def notify_backend(order_id, status, message="", is_final=False):
    """Send recharge result back to the Laravel backend.

    For final results (success/failed/rejected/etc), retries up to 5 times
    with exponential backoff. Non-final updates (processing) are fire-and-forget.

    Returns True if callback succeeded, False if all retries failed."""
    if not config.API_CALLBACK_URL:
        return True

    retries = MAX_CALLBACK_RETRIES if is_final else 1

    for attempt in range(retries):
        try:
            resp = requests.post(
                config.API_CALLBACK_URL,
                json={
                    "order_id": order_id,
                    "status": status,
                    "message": message,
                },
                headers={
                    "token": config.API_TOKEN,
                    "Content-Type": "application/json",
                },
                timeout=15,
            )
            if resp.status_code < 500:
                logging.info(f"CALLBACK {order_id}: {resp.status_code} {resp.text[:200]}")
                return True
            else:
                logging.warning(f"CALLBACK {order_id}: server error {resp.status_code} (attempt {attempt+1}/{retries})")

        except Exception as e:
            logging.error(f"CALLBACK {order_id}: failed (attempt {attempt+1}/{retries}) | {e}")

        # Wait before retry (only for final results)
        if is_final and attempt < retries - 1:
            delay = CALLBACK_RETRY_DELAYS[attempt]
            logging.info(f"CALLBACK {order_id}: retrying in {delay}s...")
            time.sleep(delay)

    logging.critical(f"CALLBACK {order_id}: ALL {retries} RETRIES FAILED — status={status}")
    return False


# =====================
# WORKER LOOP
# =====================

def worker():
    """Main worker loop: takes ONE task at a time from queue, executes recharge,
    waits for SMS confirmation, then moves to the next task.

    GUARANTEE: Only one recharge runs at a time. The modem is fully
    locked during the entire operation (USSD + SMS wait + cleanup)."""
    while True:
        task = config.task_queue.get()

        phone    = task["phone"]
        price    = task["price"]
        offer    = task["offer"]
        order_id = task["order_id"]

        logging.info(f"WORKER: === START task {order_id} for {phone} ===")

        # Update local DB to 'processing'
        database.update_order_status(order_id, "processing")

        # Notify Laravel that we started processing
        notify_backend(order_id, "processing", "Recharge started on gateway")

        try:
            # Check if modem is down before attempting
            if not config.MODEM_OK:
                logging.error(f"ORDER {order_id}: modem down, failing task.")
                result = "failed"
                raw_message = "Modem is down"
            else:
                # Execute recharge (BLOCKING — holds serial_lock the entire time)
                # modem.recharge() internally:
                #   1. Acquires serial_lock (FIRST)
                #   2. Sets RECHARGE_IN_PROGRESS = True (INSIDE lock — no race)
                #   3. Delete old SMS
                #   4. Check signal
                #   5. Check balance before
                #   6. Send USSD command
                #   7. Wait for confirmation SMS (up to 60s)
                #   8. Log SMS to message.log
                #   9. Delete SMS from SIM
                #  10. Check balance after
                #  11. Fallback: compare balances if SMS unclear
                #  12. Sets RECHARGE_IN_PROGRESS = False (INSIDE lock — no race)
                #  13. Releases serial_lock
                result, raw_message = modem.recharge(phone, price, offer)

        except Exception as e:
            logging.error(f"ORDER {order_id}: exception during recharge | {e}")
            result = "failed"
            raw_message = f"Exception: {e}"
            # Safety: clear flag in case exception happened after flag was set but before it was cleared
            config.RECHARGE_IN_PROGRESS = False

        # Update local DB with final status
        database.update_order_status(order_id, result)
        logging.info(f"ORDER {order_id} -> {result} | {raw_message}")

        # Notify Laravel backend with the final result (with retry)
        notify_backend(order_id, result, raw_message, is_final=True)

        config.task_queue.task_done()
        logging.info(f"WORKER: === END task {order_id} — waiting for next ===")


# =====================
# SELF-TEST
# =====================

if __name__ == "__main__":
    print("=" * 40)
    print("  Worker Self-Test")
    print("=" * 40)
    print(f"  Queue size     : {config.task_queue.qsize()}")
    print(f"  Modem OK       : {config.MODEM_OK}")
    print(f"  Recharge active: {config.RECHARGE_IN_PROGRESS}")
    print(f"  DB orders      : {database.count_orders()}")
    print()
    print("  To test: put a task in the queue and call worker().")
    print("  Example:")
    print('    config.task_queue.put({"order_id":"TEST001","phone":"0612345678","price":"10","offer":"2"})')
    print("    worker()  # will block until task is processed")
    print("=" * 40)
