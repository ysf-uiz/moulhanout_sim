"""
api.py — Flask routes for the recharge gateway API.

Run standalone to start just the API server (without worker/health threads):
    python api.py
"""

from flask import Flask, request, jsonify
import logging
import config
import modem
import database

# =====================
# FLASK APP
# =====================

app = Flask(__name__)


# =====================
# POST /recharge — Queue a new recharge
# =====================

@app.route("/recharge", methods=["POST"])
def api_recharge():
    token = request.headers.get("token")
    if token != config.API_TOKEN:
        return jsonify({"status": "unauthorized"}), 403

    data = request.json
    order_id = data["order_id"]
    phone    = data["phone"]
    price    = data["price"]
    offer    = data["offer"]

    # Anti-duplicate
    if database.order_exists(order_id):
        return jsonify({"status": "duplicate", "order_id": order_id})

    # Insert with 'queued' to prevent duplicates
    database.insert_order(order_id, phone, price, offer, 'queued')

    config.task_queue.put(data)

    return jsonify({
        "status": "queued",
        "order_id": order_id,
        "queue": config.task_queue.qsize()
    })


# =====================
# GET /status/<order_id> — Check recharge status
# =====================

@app.route("/status/<order_id>")
def api_status(order_id):
    status = database.get_order_status(order_id)
    if status:
        return jsonify({"status": status})
    return jsonify({"status": "processing"})


# =====================
# GET /health — Gateway health check
# =====================

@app.route("/health")
def api_health():
    # If a recharge is in progress, return cached state — do NOT touch modem
    if config.RECHARGE_IN_PROGRESS:
        return jsonify({
            "status": "busy",
            "modem": config.MODEM_OK,
            "signal": -1,
            "signal_min": config.MIN_SIGNAL,
            "registered": True,
            "creg_stat": -1,
            "queue": config.task_queue.qsize(),
            "modem_ok_flag": config.MODEM_OK,
            "sim_balance": config.SIM_BALANCE,
            "recharge_in_progress": True,
        })

    sig = 0
    modem_alive = False
    registered = False
    creg_stat = -1

    # Non-blocking: if worker holds serial, return cached state
    acquired = config.serial_lock.acquire(timeout=3)
    try:
        if acquired:
            # Double-check: recharge may have started while we waited for the lock
            if config.RECHARGE_IN_PROGRESS:
                return jsonify({
                    "status": "busy",
                    "modem": config.MODEM_OK,
                    "signal": -1,
                    "signal_min": config.MIN_SIGNAL,
                    "registered": True,
                    "creg_stat": -1,
                    "queue": config.task_queue.qsize(),
                    "modem_ok_flag": config.MODEM_OK,
                    "sim_balance": config.SIM_BALANCE,
                    "recharge_in_progress": True,
                })
            modem_alive = modem.modem_check()
            if modem_alive:
                sig = modem.get_signal()
                registered, creg_stat = modem.check_registration()
        else:
            modem_alive = config.MODEM_OK
            sig = -1
            creg_stat = -1
    except:
        pass
    finally:
        if acquired:
            config.serial_lock.release()

    # Determine status
    if not modem_alive:
        status = "down"
    elif not acquired:
        status = "busy"
    elif not registered:
        status = "no_network"
    elif sig < config.MIN_SIGNAL:
        status = "degraded"
    else:
        status = "ok"

    return jsonify({
        "status": status,
        "modem": modem_alive,
        "signal": sig,
        "signal_min": config.MIN_SIGNAL,
        "registered": registered,
        "creg_stat": creg_stat,
        "queue": config.task_queue.qsize(),
        "modem_ok_flag": config.MODEM_OK,
        "sim_balance": config.SIM_BALANCE,
        "recharge_in_progress": False,
    })


# =====================
# GET / — Simple dashboard
# =====================

@app.route("/")
def dashboard():
    total    = database.count_orders()
    success  = database.count_orders('success')
    failed   = database.count_orders('failed')
    rejected = database.count_orders('rejected')

    sig = 0
    registered = False
    creg_stat = -1
    recharging = config.RECHARGE_IN_PROGRESS

    # Only touch modem if no recharge is running
    if not recharging:
        acquired = config.serial_lock.acquire(timeout=2)
        try:
            if acquired and not config.RECHARGE_IN_PROGRESS:
                sig = modem.get_signal()
                registered, creg_stat = modem.check_registration()
        except:
            pass
        finally:
            if acquired:
                config.serial_lock.release()

    creg_labels = {0: 'Not searching', 1: 'Home', 2: 'Searching', 3: 'Denied', 5: 'Roaming'}

    return f"""
    <h2>Recharge Gateway</h2>
    <p>Total: {total}</p>
    <p>Success: {success}</p>
    <p>Failed: {failed}</p>
    <p>Rejected: {rejected}</p>
    <p>Queue: {config.task_queue.qsize()}</p>
    <p>Signal: {sig}/31{' (recharge in progress)' if recharging else ''}</p>
    <p>Network: {creg_labels.get(creg_stat, 'Unknown')} (CREG={creg_stat})</p>
    <p>Modem: {'OK' if config.MODEM_OK else 'DOWN'}</p>
    <p>Recharging: {'YES' if recharging else 'NO'}</p>
    """


# =====================
# SELF-TEST: run API server only (no worker, no health monitor)
# =====================

if __name__ == "__main__":
    print("=" * 40)
    print("  Starting API Server ONLY")
    print("  (no worker, no health monitor)")
    print("=" * 40)
    app.run(host="0.0.0.0", port=5000, debug=True)
