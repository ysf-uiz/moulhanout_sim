"""
api.py — Flask routes for the recharge gateway API.

Run standalone to start just the API server (without worker/health threads):
    python api.py
"""

from flask import Flask, request, jsonify
import html
import logging
import time
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
    now = time.time()

    for carrier, cfg in config.MODEMS.items():
        modem_inst = modem_instances.get(carrier)
        recharging = bool(cfg.get("recharge_in_progress"))
        modem_alive = bool(cfg.get("modem_ok", False))
        sig = int(cfg.get("last_signal", -1))
        registered = bool(cfg.get("last_registered", False))
        creg_stat = int(cfg.get("last_creg_stat", -1))
        last_check_ts = float(cfg.get("last_health_check_ts", 0) or 0)
        health_age_sec = int(now - last_check_ts) if last_check_ts > 0 else None

        if not modem_inst:
            modem_status = "not_initialized"
        elif recharging:
            modem_status = "busy"
        elif not modem_alive:
            modem_status = "down"
        elif not registered:
            modem_status = "no_network"
        elif sig >= 0 and sig < config.MIN_SIGNAL:
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
            "recharge_in_progress": recharging,
            "health_age_sec": health_age_sec,
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
# GET /orders — Recent orders from database (JSON)
# =====================

@app.route("/orders")
def api_orders():
    carrier = request.args.get("carrier", "").lower().strip()

    try:
        limit = int(request.args.get("limit", "50"))
    except ValueError:
        limit = 50
    limit = max(1, min(limit, 200))

    if carrier and carrier not in config.MODEMS:
        return jsonify({
            "status": "error",
            "message": f"Unknown carrier: {carrier}. Expected: {list(config.MODEMS.keys())}"
        }), 400

    rows = database.get_recent_orders(limit=limit, carrier=carrier or None)
    orders = []
    for row in rows:
        order_id, phone, price, offer, order_carrier, status, date = row
        orders.append({
            "order_id": order_id,
            "phone": phone,
            "price": price,
            "offer": offer,
            "carrier": order_carrier,
            "status": status,
            "date": date,
        })

    return jsonify({
        "status": "ok",
        "count": len(orders),
        "orders": orders,
    })


# =====================
# GET / — Simple dashboard
# =====================

@app.route("/")
def dashboard():
    carrier_filter = request.args.get("carrier", "").lower().strip()
    if carrier_filter and carrier_filter not in config.MODEMS:
        carrier_filter = ""

    try:
        limit = int(request.args.get("limit", "20"))
    except ValueError:
        limit = 20
    limit = max(1, min(limit, 100))

    total    = database.count_orders()
    success  = database.count_orders('success')
    failed   = database.count_orders('failed')
    rejected = database.count_orders('rejected')

    creg_labels = {0: 'Not searching', 1: 'Home', 2: 'Searching', 3: 'Denied', 5: 'Roaming'}

    modem_sections = ""
    for carrier, cfg in config.MODEMS.items():
        recharging = bool(cfg.get("recharge_in_progress"))
        sig = int(cfg.get("last_signal", -1))
        registered = bool(cfg.get("last_registered", False))
        creg_stat = int(cfg.get("last_creg_stat", -1))
        last_check_ts = float(cfg.get("last_health_check_ts", 0) or 0)
        health_age_sec = int(time.time() - last_check_ts) if last_check_ts > 0 else None
        signal_label = f"{sig}/31" if sig >= 0 else "N/A"

        c_total = database.count_orders(carrier=carrier)
        c_success = database.count_orders('success', carrier=carrier)
        c_failed = database.count_orders('failed', carrier=carrier)

        modem_sections += f"""
        <div style="border:1px solid #ccc; padding:10px; margin:10px; display:inline-block; vertical-align:top; min-width:250px;">
            <h3>{carrier.upper()}</h3>
            <p>Port: {cfg['serial_port']}</p>
            <p>Signal: {signal_label}{' (recharging)' if recharging else ''}</p>
            <p>Network: {creg_labels.get(creg_stat, 'Unknown')} (CREG={creg_stat})</p>
            <p>Modem: {'OK' if cfg['modem_ok'] else 'DOWN'}</p>
            <p>Balance: {cfg['sim_balance'] or 'N/A'}</p>
            <p>Recharging: {'YES' if recharging else 'NO'}</p>
            <p>Last health update: {str(health_age_sec) + 's ago' if health_age_sec is not None else 'N/A'}</p>
            <p>Queue: {cfg['task_queue'].qsize()}</p>
            <hr>
            <p>Orders: {c_total} | OK: {c_success} | Failed: {c_failed}</p>
        </div>
        """

    recent_orders = database.get_recent_orders(limit=limit, carrier=carrier_filter or None)
    table_rows = ""
    if recent_orders:
        for order_id, phone, price, offer, carrier, status, date in recent_orders:
            status_value = (status or "").lower()
            if status_value == "success":
                status_bg = "#d1fae5"
                status_fg = "#065f46"
            elif status_value in ("failed", "rejected"):
                status_bg = "#fee2e2"
                status_fg = "#991b1b"
            else:
                status_bg = "#fef3c7"
                status_fg = "#92400e"

            table_rows += f"""
            <tr>
                <td style=\"padding:8px; border-bottom:1px solid #eee;\">{html.escape(str(order_id or ''))}</td>
                <td style=\"padding:8px; border-bottom:1px solid #eee;\">{html.escape(str(phone or ''))}</td>
                <td style=\"padding:8px; border-bottom:1px solid #eee;\">{html.escape(str(price or ''))}</td>
                <td style=\"padding:8px; border-bottom:1px solid #eee;\">{html.escape(str(offer or ''))}</td>
                <td style=\"padding:8px; border-bottom:1px solid #eee;\">{html.escape(str(carrier or ''))}</td>
                <td style=\"padding:8px; border-bottom:1px solid #eee;\">
                    <span style=\"background:{status_bg}; color:{status_fg}; padding:2px 8px; border-radius:12px; font-weight:600;\">{html.escape(str(status or ''))}</span>
                </td>
                <td style=\"padding:8px; border-bottom:1px solid #eee;\">{html.escape(str(date or ''))}</td>
            </tr>
            """
    else:
        table_rows = """
        <tr>
            <td colspan=\"7\" style=\"padding:12px; text-align:center; color:#666;\">No orders found.</td>
        </tr>
        """

    carrier_options = "<option value=''>All carriers</option>"
    for carrier in config.MODEMS.keys():
        selected = "selected" if carrier == carrier_filter else ""
        carrier_options += f"<option value='{carrier}' {selected}>{carrier.upper()}</option>"

    return f"""
    <h2>Recharge Gateway</h2>
    <div>{modem_sections}</div>
    <hr>
    <h3>Totals</h3>
    <p>Total: {total} | Success: {success} | Failed: {failed} | Rejected: {rejected}</p>
    <hr>
    <h3>Recent Orders</h3>
    <form method="GET" style="margin-bottom:12px;">
        <label>Carrier:</label>
        <select name="carrier">{carrier_options}</select>
        <label style="margin-left:8px;">Limit:</label>
        <input type="number" name="limit" min="1" max="100" value="{limit}" style="width:80px;">
        <button type="submit">Filter</button>
    </form>
    <div style="overflow-x:auto; border:1px solid #ddd; border-radius:8px;">
        <table style="width:100%; border-collapse:collapse;">
            <thead style="background:#f6f6f6; text-align:left;">
                <tr>
                    <th style="padding:8px;">Order ID</th>
                    <th style="padding:8px;">Phone</th>
                    <th style="padding:8px;">Price</th>
                    <th style="padding:8px;">Offer</th>
                    <th style="padding:8px;">Carrier</th>
                    <th style="padding:8px;">Status</th>
                    <th style="padding:8px;">Updated At</th>
                </tr>
            </thead>
            <tbody>
                {table_rows}
            </tbody>
        </table>
    </div>
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
