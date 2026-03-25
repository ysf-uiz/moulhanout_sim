"""
worker.py — Queue consumer that processes recharge tasks ONE AT A TIME per modem.

Each modem gets its own worker thread consuming from its own queue.
The worker holds the modem lock for the entire recharge lifecycle:
  lock -> send USSD -> wait SMS -> log -> cleanup -> unlock -> next

Run standalone to show worker info:
    python worker.py
"""

import time
import logging
import requests
import config
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

def worker(modem_instance):
    """Worker loop for a specific modem. Consumes from that modem's queue.

    GUARANTEE: Only one recharge runs at a time per modem. The modem is fully
    locked during the entire operation (USSD + SMS wait + cleanup)."""
    carrier = modem_instance.carrier
    task_queue = modem_instance.cfg["task_queue"]

    while True:
        task = task_queue.get()

        phone    = task["phone"]
        price    = task["price"]
        offer    = task["offer"]
        order_id = task["order_id"]
        queued_at = task.get("queued_at")

        try:
            queued_at_ts = float(queued_at)
        except (TypeError, ValueError):
            queued_at_ts = time.time()

        waited_sec = max(0, int(time.time() - queued_at_ts))
        max_wait_sec = max(1, int(getattr(config, "MAX_QUEUE_WAIT_SEC", 300)))

        # If a request sits in queue too long, reject it without touching the modem.
        if waited_sec > max_wait_sec:
            timeout_msg = (
                f"Queue timeout exceeded ({waited_sec}s > {max_wait_sec}s). "
                "Recharge rejected before sending to modem"
            )
            timed_out = database.update_order_status_if(
                order_id,
                "rejected",
                ["queued", "pending"],
            )
            if timed_out:
                logging.warning(f"[{carrier}] ORDER {order_id}: {timeout_msg}")
                notify_backend(order_id, "rejected", timeout_msg, is_final=True)
            else:
                current = database.get_order_status(order_id)
                logging.info(
                    f"[{carrier}] ORDER {order_id}: timeout check skipped (current_status={current})"
                )
            task_queue.task_done()
            continue

        # Atomically claim job for processing. If it was cancelled in the meantime,
        # this will fail and the worker must skip sending.
        claimed = database.claim_order_for_processing(order_id)
        if not claimed:
            current = database.get_order_status(order_id)
            logging.info(
                f"[{carrier}] ORDER {order_id}: skipped (not claimable, current_status={current})"
            )
            task_queue.task_done()
            continue

        logging.info(f"[{carrier}] WORKER: === START task {order_id} for {phone} ===")

        # Notify Laravel that we started processing
        notify_backend(order_id, "processing", "Recharge started on gateway")

        try:
            # Check if modem is down before attempting
            if not modem_instance.cfg["modem_ok"]:
                logging.error(f"[{carrier}] ORDER {order_id}: modem down, failing task.")
                result = "failed"
                raw_message = "Modem is down"
            else:
                result, raw_message = modem_instance.recharge(phone, price, offer)

        except Exception as e:
            logging.error(f"[{carrier}] ORDER {order_id}: exception during recharge | {e}")
            result = "failed"
            raw_message = f"Exception: {e}"
            modem_instance.cfg["recharge_in_progress"] = False

        # Update local DB with final status
        database.update_order_status(order_id, result)
        logging.info(f"[{carrier}] ORDER {order_id} -> {result} | {raw_message}")

        # Refresh balance only when queue is drained, so pending orders are not delayed.
        if task_queue.empty():
            serial_lock = modem_instance.cfg.get("serial_lock")
            acquired = True
            if serial_lock is not None:
                acquired = serial_lock.acquire(timeout=5)

            try:
                if not acquired:
                    logging.info(f"[{carrier}] ORDER {order_id}: post-recharge balance skipped (modem busy)")
                elif modem_instance.cfg.get("recharge_in_progress"):
                    logging.info(f"[{carrier}] ORDER {order_id}: post-recharge balance skipped (recharge active)")
                else:
                    modem_instance.check_balance()
            except Exception as e:
                logging.warning(f"[{carrier}] ORDER {order_id}: post-recharge balance check failed | {e}")
            finally:
                if serial_lock is not None and acquired:
                    serial_lock.release()
        else:
            logging.info(
                f"[{carrier}] ORDER {order_id}: post-recharge balance skipped "
                f"(pending queue={task_queue.qsize()})"
            )

        # Notify Laravel backend with the final result (with retry)
        notify_backend(order_id, result, raw_message, is_final=True)

        task_queue.task_done()
        logging.info(f"[{carrier}] WORKER: === END task {order_id} — waiting for next ===")


# =====================
# SELF-TEST
# =====================

if __name__ == "__main__":
    print("=" * 40)
    print("  Worker Self-Test")
    print("=" * 40)
    for carrier, cfg in config.MODEMS.items():
        print(f"\n  [{carrier.upper()}]")
        print(f"    Queue size     : {cfg['task_queue'].qsize()}")
        print(f"    Modem OK       : {cfg['modem_ok']}")
        print(f"    Recharge active: {cfg['recharge_in_progress']}")
    print(f"\n  DB orders      : {database.count_orders()}")
    print()
    print("  To test: put a task in a modem's queue and call worker(modem_instance).")
    print("=" * 40)
