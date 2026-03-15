"""
api.py — Flask routes for the recharge gateway API.

Run standalone to start just the API server (without worker/health threads):
    python api.py
"""

from flask import Flask, request, jsonify
import logging
import config
import database

# =====================
# FLASK APP
# =====================

app = Flask(__name__)

# Populated by index.py after creating Modem instances
modem_instances = {}  # {"orange": Modem, "inwi": Modem}


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
    carrier  = data.get("carrier", "").lower()

    if carrier not in config.MODEMS:
        return jsonify({"status": "error", "message": f"Unknown carrier: {carrier}. Expected: {list(config.MODEMS.keys())}"}), 400

    # Anti-duplicate
    if database.order_exists(order_id):
        return jsonify({"status": "duplicate", "order_id": order_id})

    # Insert with 'queued' to prevent duplicates
    database.insert_order(order_id, phone, price, offer, 'queued', carrier=carrier)

    config.MODEMS[carrier]["task_queue"].put(data)

    return jsonify({
        "status": "queued",
        "order_id": order_id,
        "carrier": carrier,
        "queue": config.MODEMS[carrier]["task_queue"].qsize()
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
# GET /health — Gateway health check (all modems)
# =====================

@app.route("/health")
def api_health():
    result = {"modems": {}}

    for carrier, cfg in config.MODEMS.items():
        modem_inst = modem_instances.get(carrier)

        # If recharge is in progress, return cached state for this modem
        if cfg["recharge_in_progress"]:
            result["modems"][carrier] = {
                "status": "busy",
                "modem": cfg["modem_ok"],
                "signal": -1,
                "registered": True,
                "creg_stat": -1,
                "queue": cfg["task_queue"].qsize(),
                "sim_balance": cfg["sim_balance"],
                "recharge_in_progress": True,
            }
            continue

        sig = 0
        modem_alive = False
        registered = False
        creg_stat = -1
        modem_status = "unknown"

        if modem_inst:
            acquired = cfg["serial_lock"].acquire(timeout=3)
            try:
                if acquired:
                    if cfg["recharge_in_progress"]:
                        result["modems"][carrier] = {
                            "status": "busy",
                            "modem": cfg["modem_ok"],
                            "signal": -1,
                            "registered": True,
                            "creg_stat": -1,
                            "queue": cfg["task_queue"].qsize(),
                            "sim_balance": cfg["sim_balance"],
                            "recharge_in_progress": True,
                        }
                        continue
                    modem_alive = modem_inst.modem_check()
                    if modem_alive:
                        sig = modem_inst.get_signal()
                        registered, creg_stat = modem_inst.check_registration()
                else:
                    modem_alive = cfg["modem_ok"]
                    sig = -1
                    creg_stat = -1
            except:
                pass
            finally:
                if acquired:
                    cfg["serial_lock"].release()

        if not modem_inst:
            modem_status = "not_initialized"
        elif not modem_alive:
            modem_status = "down"
        elif not acquired:
            modem_status = "busy"
        elif not registered:
            modem_status = "no_network"
        elif sig < config.MIN_SIGNAL:
            modem_status = "degraded"
        else:
            modem_status = "ok"

        result["modems"][carrier] = {
            "status": modem_status,
            "modem": modem_alive,
            "signal": sig,
            "signal_min": config.MIN_SIGNAL,
            "registered": registered,
            "creg_stat": creg_stat,
            "queue": cfg["task_queue"].qsize(),
            "sim_balance": cfg["sim_balance"],
            "recharge_in_progress": False,
        }

    # Top-level status
    statuses = [m["status"] for m in result["modems"].values()]
    if all(s == "down" for s in statuses):
        result["status"] = "down"
    elif any(s == "ok" for s in statuses):
        result["status"] = "ok"
    else:
        result["status"] = "degraded"

    return jsonify(result)


# =====================
# GET / — Simple dashboard
# =====================

@app.route("/")
def dashboard():
    total    = database.count_orders()
    success  = database.count_orders('success')
    failed   = database.count_orders('failed')
    rejected = database.count_orders('rejected')

    creg_labels = {0: 'Not searching', 1: 'Home', 2: 'Searching', 3: 'Denied', 5: 'Roaming'}

    modem_sections = ""
    for carrier, cfg in config.MODEMS.items():
        modem_inst = modem_instances.get(carrier)
        recharging = cfg["recharge_in_progress"]
        sig = 0
        registered = False
        creg_stat = -1

        if modem_inst and not recharging:
            acquired = cfg["serial_lock"].acquire(timeout=2)
            try:
                if acquired and not cfg["recharge_in_progress"]:
                    sig = modem_inst.get_signal()
                    registered, creg_stat = modem_inst.check_registration()
            except:
                pass
            finally:
                if acquired:
                    cfg["serial_lock"].release()

        c_total = database.count_orders(carrier=carrier)
        c_success = database.count_orders('success', carrier=carrier)
        c_failed = database.count_orders('failed', carrier=carrier)

        modem_sections += f"""
        <div style="border:1px solid #ccc; padding:10px; margin:10px; display:inline-block; vertical-align:top; min-width:250px;">
            <h3>{carrier.upper()}</h3>
            <p>Port: {cfg['serial_port']}</p>
            <p>Signal: {sig}/31{' (recharging)' if recharging else ''}</p>
            <p>Network: {creg_labels.get(creg_stat, 'Unknown')} (CREG={creg_stat})</p>
            <p>Modem: {'OK' if cfg['modem_ok'] else 'DOWN'}</p>
            <p>Balance: {cfg['sim_balance'] or 'N/A'}</p>
            <p>Recharging: {'YES' if recharging else 'NO'}</p>
            <p>Queue: {cfg['task_queue'].qsize()}</p>
            <hr>
            <p>Orders: {c_total} | OK: {c_success} | Failed: {c_failed}</p>
        </div>
        """

    return f"""
    <h2>Recharge Gateway</h2>
    <div>{modem_sections}</div>
    <hr>
    <h3>Totals</h3>
    <p>Total: {total} | Success: {success} | Failed: {failed} | Rejected: {rejected}</p>
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
