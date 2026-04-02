"""
api.py — Flask routes for the recharge gateway API.

Run standalone to start just the API server (without worker/health threads):
    python api.py
"""

from flask import Flask, request, jsonify, redirect, url_for
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


def _clean_message(value, limit=220):
    text = " ".join(str(value or "").split())
    return text[:limit]


def _is_admin_authorized(req):
    """Require both gateway token and admin token for privileged actions."""
    token = req.headers.get("token")
    admin_token = req.headers.get("admin-token") or req.headers.get("x-admin-token")

    if token != config.API_TOKEN:
        return False, (jsonify({"status": "unauthorized"}), 403)

    if not config.ADMIN_TOKEN:
        return False, (
            jsonify({
                "status": "forbidden",
                "message": "admin token is not configured on gateway",
            }),
            403,
        )

    if admin_token != config.ADMIN_TOKEN:
        return False, (jsonify({"status": "forbidden"}), 403)

    return True, None


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

    queued_task = {
        "order_id": order_id,
        "phone": phone,
        "price": price,
        "offer": offer,
        "carrier": carrier,
        "queued_at": time.time(),
    }
    config.MODEMS[carrier]["task_queue"].put(queued_task)

    return jsonify({
        "status": "queued",
        "order_id": order_id,
        "carrier": carrier,
        "queue": config.MODEMS[carrier]["task_queue"].qsize()
    })


# =====================
# POST /cancel — Cancel a queued recharge before sending
# =====================

@app.route("/cancel", methods=["POST"])
def api_cancel():
    token = request.headers.get("token")
    if token != config.API_TOKEN:
        return jsonify({"status": "unauthorized"}), 403

    data = request.json or {}
    order_id = (data.get("order_id") or "").strip()
    if not order_id:
        return jsonify({"status": "error", "message": "order_id is required"}), 422

    current = (database.get_order_status(order_id) or "").lower()
    if not current:
        return jsonify({"status": "not_found", "order_id": order_id}), 404

    if current == "cancelled":
        return jsonify({"status": "already_cancelled", "order_id": order_id})

    if current in ("processing", "success", "failed", "rejected", "balance_error"):
        return jsonify({
            "status": "cannot_cancel",
            "order_id": order_id,
            "current_status": current,
        })

    cancelled = database.update_order_status_if(order_id, "cancelled", ["queued", "pending"])
    if cancelled:
        return jsonify({"status": "cancelled", "order_id": order_id})

    # Race-safe fallback: status changed between read and update.
    current = (database.get_order_status(order_id) or "unknown").lower()
    return jsonify({
        "status": "cannot_cancel",
        "order_id": order_id,
        "current_status": current,
    })


# =====================
# POST /admin/orange/topup — Admin-only Orange SIM top-up
# =====================

@app.route("/admin/orange/topup", methods=["POST"])
def api_admin_orange_topup():
    ok, error_response = _is_admin_authorized(request)
    if not ok:
        return error_response

    data = request.json or {}
    code = (data.get("code") or "").strip()
    if not code:
        return jsonify({"status": "error", "message": "code is required"}), 422

    modem = modem_instances.get("orange")
    if not modem:
        return jsonify({"status": "error", "message": "orange modem is not initialized"}), 503

    orange_queue = config.MODEMS["orange"]["task_queue"]
    if modem.cfg.get("recharge_in_progress"):
        return jsonify({
            "status": "busy",
            "message": "orange modem is currently processing a recharge",
        }), 409

    if orange_queue.qsize() > 0:
        return jsonify({
            "status": "busy",
            "message": "orange queue is not empty; try after pending recharges are done",
            "queue": orange_queue.qsize(),
        }), 409

    success, response_text = modem.orange_topup_sim(code)
    if not success:
        return jsonify({
            "status": "failed",
            "message": response_text,
        }), 400

    return jsonify({
        "status": "sent",
        "message": "orange top-up command sent successfully",
        "modem_response": response_text,
    })


# =====================
# POST /view/orange/sim-recharge — Dashboard top-up (no API token)
# =====================

@app.route("/view/orange/sim-recharge", methods=["POST"])
def view_orange_sim_recharge():
    code = (request.form.get("code") or "").strip().replace(" ", "")
    if not code:
        return redirect(url_for("dashboard", topup_status="error", topup_message="code is required"))

    modem = modem_instances.get("orange")
    if not modem:
        return redirect(url_for("dashboard", topup_status="error", topup_message="orange modem is not initialized"))

    orange_queue = config.MODEMS["orange"]["task_queue"]
    if modem.cfg.get("recharge_in_progress"):
        return redirect(url_for("dashboard", topup_status="error", topup_message="orange modem is busy"))

    if orange_queue.qsize() > 0:
        return redirect(url_for(
            "dashboard",
            topup_status="error",
            topup_message=f"orange queue not empty (queue={orange_queue.qsize()})",
        ))

    success, response_text = modem.orange_topup_sim(code)
    safe_message = _clean_message(str(response_text).replace(code, "***"))

    if not success:
        return redirect(url_for("dashboard", topup_status="error", topup_message=safe_message or "top-up failed"))

    return redirect(url_for("dashboard", topup_status="success", topup_message="orange top-up command sent"))


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

    topup_status = request.args.get("topup_status", "").lower().strip()
    topup_message = _clean_message(request.args.get("topup_message", ""))

    if topup_status == "success":
        topup_banner = (
            f"<div style='background:#dcfce7;color:#166534;padding:10px 12px;border-radius:8px;"
            f"border:1px solid #86efac;margin:10px 0;'>{html.escape(topup_message or 'Top-up sent')}</div>"
        )
    elif topup_status == "error":
        topup_banner = (
            f"<div style='background:#fee2e2;color:#991b1b;padding:10px 12px;border-radius:8px;"
            f"border:1px solid #fca5a5;margin:10px 0;'>{html.escape(topup_message or 'Top-up failed')}</div>"
        )
    else:
        topup_banner = ""

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
    <h3>Orange SIM Top-up (Dashboard only)</h3>
    <p>Send directly from this system view without API token headers.</p>
    {topup_banner}
    <form method="POST" action="/view/orange/sim-recharge" style="display:flex; gap:8px; align-items:center; margin:10px 0 16px; flex-wrap:wrap;">
        <label for="sim-code" style="font-weight:600;">Recharge code:</label>
        <input id="sim-code" name="code" type="text" inputmode="numeric" pattern="[0-9]{6,32}" minlength="6" maxlength="32" required placeholder="ex: 123456789012" style="padding:8px 10px; border:1px solid #ccc; border-radius:6px; min-width:260px;">
        <button type="submit" style="padding:8px 14px; border:none; border-radius:6px; background:#2563eb; color:#fff; cursor:pointer;">Send top-up</button>
    </form>
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
