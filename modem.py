"""
modem.py — SIM800L modem control: AT commands, signal, CREG, SMS, recharge.

Run standalone to test the modem without the full server:
    python modem.py
"""

import serial
import time
import re
import logging
import config

# =====================
# SERIAL PORT
# =====================

ser = serial.Serial(config.SERIAL_PORT, config.BAUDRATE, timeout=1)


# =====================
# AT COMMAND
# =====================

def send_at(cmd, wait=2):
    """Send an AT command and return the response."""
    ser.write((cmd + "\r").encode())
    time.sleep(wait)
    resp = ser.read_all().decode(errors="ignore")
    logging.info(f"AT {cmd} | {resp}")
    return resp


# =====================
# SMS LOGGING & CLEANUP
# =====================

def log_sms(sender, message):
    """Log SMS to message.log with timestamp and sender."""
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    log_entry = f"{timestamp} | FROM: {sender} | MSG: {message}\n"
    try:
        with open("message.log", "a", encoding="utf-8") as f:
            f.write(log_entry)
        logging.info(f"SMS LOGGED: {sender}")
    except Exception as e:
        logging.error(f"SMS LOG error: {e}")


def delete_all_sms():
    """Delete all SMS messages to prevent inbox from filling up."""
    try:
        send_at('AT+CMGF=1', 1)
        resp = send_at('AT+CMGDA="DEL ALL"', 3)
        if "OK" in resp:
            logging.info("SMS: all messages deleted")
        else:
            logging.warning(f"SMS: delete failed | {resp}")
    except Exception as e:
        logging.error(f"SMS: cleanup error | {e}")


# =====================
# SIGNAL CHECK
# =====================

def get_signal():
    """Return signal strength (0-31, 99=unknown). 0=no signal."""
    try:
        resp = send_at("AT+CSQ", 1)
        if "+CSQ:" in resp:
            csq_line = resp.split("+CSQ:")[1].split("\n")[0].strip()
            parts = csq_line.split(",")
            return int(parts[0].strip())
    except:
        pass
    return 0


def check_registration():
    """Check network registration. Returns (registered: bool, stat: int).
    CREG stat: 0=not searching, 1=home, 2=searching, 3=denied, 5=roaming.
    Only 1 (home) and 5 (roaming) mean the modem can make USSD calls."""
    try:
        resp = send_at("AT+CREG?", 1)
        if "+CREG:" in resp:
            # Take only the CREG line (before \r\n / OK)
            creg_line = resp.split("+CREG:")[1].split("\n")[0].strip()
            parts = creg_line.split(",")
            stat = int(parts[1].strip()) if len(parts) > 1 else int(parts[0].strip())
            registered = stat in (1, 5)
            if not registered:
                logging.warning(f"CREG: not registered (stat={stat})")
            return registered, stat
    except Exception as e:
        logging.error(f"CREG: check error | {e}")
    return False, -1


def check_balance():
    """Check SIM balance via USSD *580#. Returns balance as float or None."""
    try:
        send_at('AT+CMGF=1', 1)
        send_at('AT+CUSD=1,"*580#",15', 5)

        # Read USSD response
        start = time.time()
        collected = ""
        while time.time() - start < 10:
            if ser.in_waiting:
                line = ser.readline().decode(errors="ignore")
                collected += line
                if "+CUSD:" in collected:
                    # Wait a bit more for full response
                    time.sleep(1)
                    if ser.in_waiting:
                        collected += ser.read_all().decode(errors="ignore")
                    break
            time.sleep(0.5)

        logging.info(f"BALANCE RAW: {collected}")

        # Parse balance from response
        # Common patterns: "Solde: 12.50 DH", "12,50 DH", "12.50DH"
        match = re.search(r'(\d+[.,]\d+)\s*(?:DH|MAD|dh|mad)', collected)
        if match:
            balance = float(match.group(1).replace(',', '.'))
            config.SIM_BALANCE = balance
            logging.info(f"BALANCE: {balance} MAD")
            return balance

        # Try integer pattern: "12 DH"
        match = re.search(r'(\d+)\s*(?:DH|MAD|dh|mad)', collected)
        if match:
            balance = float(match.group(1))
            config.SIM_BALANCE = balance
            logging.info(f"BALANCE: {balance} MAD")
            return balance

        logging.warning(f"BALANCE: could not parse | {collected}")
        return None
    except Exception as e:
        logging.error(f"BALANCE: error | {e}")
        return None


def has_signal():
    """Check if modem has minimum usable signal AND is registered on network."""
    sig = get_signal()
    sig_ok = sig >= config.MIN_SIGNAL and sig != 99
    if not sig_ok:
        logging.warning(f"SIGNAL: too low ({sig}/31, min={config.MIN_SIGNAL})")
        return False

    registered, stat = check_registration()
    if not registered:
        logging.warning(f"SIGNAL: ok ({sig}/31) but not registered (CREG stat={stat})")
        return False

    return True


# =====================
# MODEM CHECK & RECOVERY
# =====================

def modem_check():
    """Check if modem is alive and responding."""
    try:
        r = send_at("AT", 1)
        return "OK" in r
    except:
        return False


def force_register():
    """Force the modem to search and register on the network.
    Use when CREG=0 (not searching) or CREG=3 (denied)."""
    logging.info("MODEM: forcing network registration...")
    try:
        # Set automatic operator selection
        send_at("AT+COPS=0", 5)
        time.sleep(5)
        # Check if it worked
        registered, stat = check_registration()
        if registered:
            logging.info(f"MODEM: network registration recovered (CREG={stat})")
            return True
        # If still not registered, try full modem restart
        logging.warning(f"MODEM: AT+COPS=0 didn't help (CREG={stat}), doing full reset")
        return modem_reset()
    except Exception as e:
        logging.error(f"MODEM: force_register error | {e}")
        return False


def modem_reset():
    """Attempt to reset the modem via AT command."""
    logging.warning("MODEM: attempting reset...")
    try:
        send_at("AT+CFUN=1,1", 5)
        time.sleep(10)
        if modem_check():
            logging.info("MODEM: reset successful")
            config.MODEM_OK = True
            delete_all_sms()
            return True
        else:
            logging.error("MODEM: reset failed, still not responding")
            config.MODEM_OK = False
            return False
    except:
        logging.error("MODEM: reset error")
        config.MODEM_OK = False
        return False


def modem_health_monitor():
    """Background thread: check modem health every 60s. Auto-recover.
    SKIPS entirely when a recharge is in progress to avoid modem interference.

    Lock discipline: NEVER sends AT commands if RECHARGE_IN_PROGRESS is True.
    The flag is set/cleared INSIDE serial_lock by modem.recharge(), so checking
    after acquiring the lock guarantees no race condition."""
    while True:
        time.sleep(60)

        # Fast-path: skip without even trying to acquire the lock
        if config.RECHARGE_IN_PROGRESS:
            logging.info("MODEM HEALTH: skipped — recharge in progress")
            continue

        acquired = config.serial_lock.acquire(timeout=5)
        if not acquired:
            continue
        try:
            # Double-check INSIDE lock: recharge may have started between
            # our fast-path check and acquiring the lock
            if config.RECHARGE_IN_PROGRESS:
                logging.info("MODEM HEALTH: skipped — recharge started while waiting for lock")
                continue

            if modem_check():
                config.MODEM_OK = True
                registered, stat = check_registration()
                if not registered:
                    logging.warning(f"MODEM: alive but not registered (CREG={stat})")
                    if stat in (0, 3):
                        force_register()
                else:
                    check_balance()
            else:
                logging.error("MODEM: health check failed")
                config.MODEM_OK = False
                modem_reset()
        except Exception as e:
            logging.error(f"MODEM: health monitor error | {e}")
            config.MODEM_OK = False
        finally:
            config.serial_lock.release()


# =====================
# SMS PARSER
# =====================

def read_sms(timeout=60, pre_collected=""):
    """Wait for an incoming SMS and parse the recharge result.
    Returns (status, raw_message) tuple.
    Uses ser.read() for robust reading — reads only what's available.
    ALL received SMS are logged to message.log and deleted from SIM.

    pre_collected: data already read from serial (e.g., if +CMT arrived
                   during ATD response). Avoids losing fast SMS."""
    start = time.time()
    collected = pre_collected or ""
    logging.info(f"SMS: waiting for confirmation (timeout={timeout}s, pre={len(collected)} bytes)...")

    while time.time() - start < timeout:
        if ser.in_waiting:
            chunk = ser.read(ser.in_waiting).decode(errors="ignore")
            collected += chunk
            logging.info(f"SMS RAW CHUNK: {repr(chunk)}")

            if "+CMT:" in collected:
                # +CMT header found — wait a bit more for the body
                time.sleep(2)
                if ser.in_waiting:
                    extra = ser.read(ser.in_waiting).decode(errors="ignore")
                    collected += extra
                    logging.info(f"SMS RAW EXTRA: {repr(extra)}")

                sender = "Unknown"
                match = re.search(r'\+CMT:\s*"([^"]*)"', collected)
                if match:
                    sender = match.group(1)

                body_match = re.search(r'\+CMT:.*?\r?\n(.+)', collected, re.DOTALL)
                body = body_match.group(1).strip() if body_match else collected

                log_sms(sender, body)
                # NOTE: do NOT call delete_all_sms() here — it uses send_at()
                # which would read_all() and interfere with serial state.
                # Cleanup happens after recharge() finishes via send_at("ATH")
                # and the next delete_all_sms() at the start of the next recharge.

                low = body.lower()
                if "insuffisant" in low:
                    return "balance_error", body
                if "succes" in low or "success" in low:
                    return "success", body
                if any(kw in low for kw in ["rejete", "refuse", "erreur", "echoue", "failure", "failed"]):
                    return "rejected", body
                return "unknown", body

        time.sleep(0.5)

    # Timeout: no SMS received — log all raw data for debugging
    logging.warning(f"SMS: timeout after {timeout}s — no confirmation received")
    logging.warning(f"SMS: raw collected data: {repr(collected)}")
    return "unknown", "TIMEOUT: No SMS received"


# =====================
# RECHARGE (USSD)
# =====================

def _flush_serial():
    """Drain any leftover data from the serial buffer."""
    if ser.in_waiting:
        ser.read_all()


def _send_raw(cmd, wait=0.5):
    """Send an AT command without reading response — avoids eating SMS data.
    Use this instead of send_at() during the recharge flow."""
    ser.write((cmd + "\r").encode())
    time.sleep(wait)


def _send_raw_and_wait_ok(cmd, timeout=3):
    """Send an AT command and wait for OK/ERROR — reads ONLY the command response.
    Stops reading as soon as it sees OK or ERROR, so it won't eat SMS data."""
    ser.write((cmd + "\r").encode())
    start = time.time()
    collected = ""
    while time.time() - start < timeout:
        if ser.in_waiting:
            chunk = ser.read(ser.in_waiting).decode(errors="ignore")
            collected += chunk
            if "OK" in collected or "ERROR" in collected:
                break
        time.sleep(0.1)
    logging.info(f"RAW_AT {cmd} | {collected.strip()}")
    return collected


def recharge(phone, price, offer):
    """Execute a recharge via USSD. Returns (status, message) tuple.
    Uses balance check before/after as fallback when SMS is unclear.

    IMPORTANT: Acquires serial_lock FIRST, then sets RECHARGE_IN_PROGRESS.
    This eliminates the race window where other components could sneak
    AT commands between the flag and the lock.

    CRITICAL: After sending ATD, we NEVER call send_at() or read_all()
    until read_sms() has captured the confirmation. Any read_all() would
    eat the +CMT: notification from the serial buffer."""
    code = f"1391997{phone}{price}*{offer}"

    with config.serial_lock:
        config.RECHARGE_IN_PROGRESS = True
        try:
            logging.info(f"RECHARGE START: {phone} {price} MAD (offer={offer})")

            # 1. Pre-cleanup (send_at is safe here — no SMS expected yet)
            delete_all_sms()

            if not has_signal():
                return "no_signal", "No signal or not registered on network"

            # 2. Check balance BEFORE (send_at is safe here — no SMS expected yet)
            balance_before = check_balance()
            logging.info(f"RECHARGE: balance before = {balance_before}")

            # 3. Configure SMS reporting (push CMT directly to serial)
            #    Use send_at here — still safe, no SMS coming yet
            send_at("AT+CMGF=1", 1)
            send_at("AT+CNMI=2,2,0,0,0", 1)

            # 4. Flush serial buffer — clean slate before USSD
            _flush_serial()

            # 5. Send the recharge command
            #    CRITICAL: from here until read_sms() returns, we must NOT
            #    call send_at() or read_all() — those would eat the SMS!
            logging.info(f"RECHARGE: sending USSD command: {code}")
            ser.write((f"ATD {code};\r").encode())

            # Wait for SIM800L to acknowledge the dial command
            # The modem sends "OK" or "NO CARRIER" — we read ONLY that
            time.sleep(1)
            atd_response = ""
            if ser.in_waiting:
                atd_response = ser.read(ser.in_waiting).decode(errors="ignore")
                # Only consume the ATD echo/response, stop before any +CMT
                if "+CMT:" in atd_response:
                    # Rare: SMS arrived extremely fast — put it back conceptually
                    # by passing it to read_sms as pre-collected data
                    logging.info(f"RECHARGE: SMS arrived during ATD response!")
                else:
                    logging.info(f"RECHARGE: ATD response: {atd_response.strip()}")
                    atd_response = ""  # Clear it — not part of SMS

            # 6. Wait for confirmation SMS
            status, message = read_sms(timeout=60, pre_collected=atd_response)

            # 7. Dialing done, hang up just in case — NOW safe to use send_at
            send_at("ATH", 1)

            # 8. Check balance AFTER
            balance_after = check_balance()
            logging.info(f"RECHARGE: balance after = {balance_after}, sms_status = {status}")

            # Build info string
            balance_info = ""
            if balance_before is not None and balance_after is not None:
                diff = balance_before - balance_after
                balance_info = f" | Solde: {balance_after} MAD (diff: {diff:.2f})"

            # 7. Fallback: if SMS was unclear, use balance to decide
            if status == "unknown" and balance_before is not None and balance_after is not None:
                diff = balance_before - balance_after
                if diff >= float(price) * 0.8:  # allow small margin
                    status = "success"
                    message = (message or "") + f" [BALANCE CHECK: -{diff:.2f} MAD]"
                    logging.info(f"RECHARGE: SMS unknown but balance dropped {diff:.2f} → SUCCESS")
                else:
                    status = "failed"
                    message = (message or "") + f" [BALANCE CHECK: no change]"
                    logging.info(f"RECHARGE: SMS unknown and balance unchanged → FAILED")

            message = (message or "") + balance_info
            logging.info(f"RECHARGE FINISHED: {status} | {message}")

        finally:
            # ALWAYS clear flag INSIDE the lock — even on early return or exception
            config.RECHARGE_IN_PROGRESS = False

    return status, message


# =====================
# SELF-TEST
# =====================

if __name__ == "__main__":
    CREG_LABELS = {0: 'Not searching', 1: 'Home', 2: 'Searching', 3: 'Denied', 5: 'Roaming'}

    print("=" * 40)
    print("  Modem Self-Test")
    print("=" * 40)

    with config.serial_lock:
        # 1. Basic AT check
        alive = modem_check()
        print(f"  Modem alive : {'YES ✓' if alive else 'NO ✗'}")

        if alive:
            # 2. Signal strength
            sig = get_signal()
            print(f"  Signal (CSQ): {sig}/31 {'✓' if sig >= config.MIN_SIGNAL else '✗ LOW'}")

            # 3. Network registration
            registered, stat = check_registration()
            label = CREG_LABELS.get(stat, f'Unknown({stat})')
            print(f"  Network CREG: {label} {'✓' if registered else '✗ NOT REGISTERED'}")

            # 4. SMS cleanup
            delete_all_sms()
            print(f"  SMS cleanup : done ✓")

    print("=" * 40)
    print(f"  Ready to recharge: {'YES ✓' if alive and sig >= config.MIN_SIGNAL and registered else 'NO ✗'}")
