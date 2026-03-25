"""
modem.py — SIM800L modem control: AT commands, signal, CREG, SMS, recharge.

Run standalone to test a specific modem without the full server:
    python modem.py          # test IAM (default)
    python modem.py inwi     # test Inwi
"""

import serial
import time
import re
import json
import logging
import urllib.parse
import urllib.request
import config


class Modem:
    """Controls a single SIM800L modem on a given serial port."""

    def __init__(self, carrier, cfg):
        """
        carrier: "orange" or "inwi"
        cfg: dict from config.MODEMS[carrier]
        """
        self.carrier = carrier
        self.cfg = cfg
        self.ser = serial.Serial(cfg["serial_port"], cfg["baudrate"], timeout=1)
        self.serial_lock = cfg["serial_lock"]
        self.recharge_code_template = cfg["recharge_code_template"]
        self.balance_ussd = cfg["balance_ussd"]
        self.cfg.setdefault("last_signal", -1)
        self.cfg.setdefault("last_registered", False)
        self.cfg.setdefault("last_creg_stat", -1)
        self.cfg.setdefault("last_health_check_ts", 0.0)
        self.cfg.setdefault("offline_since_ts", 0.0)
        self.cfg.setdefault("offline_alert_sent", False)
        self.cfg.setdefault("offline_alert_last_try_ts", 0.0)
        self.cfg.setdefault("telegram_chat_id_cache", "")

    # =====================
    # AT COMMAND
    # =====================

    def send_at(self, cmd, wait=2):
        """Send an AT command and return the response."""
        self.ser.write((cmd + "\r").encode())
        time.sleep(wait)
        resp = self.ser.read_all().decode(errors="ignore")
        logging.info(f"[{self.carrier}] AT {cmd} | {resp}")
        return resp

    # =====================
    # SMS LOGGING & CLEANUP
    # =====================

    def log_sms(self, sender, message):
        """Log SMS to message.log with timestamp, carrier and sender."""
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        log_entry = f"{timestamp} | [{self.carrier}] FROM: {sender} | MSG: {message}\n"
        try:
            with open("message.log", "a", encoding="utf-8") as f:
                f.write(log_entry)
            logging.info(f"[{self.carrier}] SMS LOGGED: {sender}")
        except Exception as e:
            logging.error(f"[{self.carrier}] SMS LOG error: {e}")

    def _save_all_sms(self):
        """Read all stored SMS and log them to message.log before deletion."""
        try:
            resp = self.send_at('AT+CMGL="ALL"', 3)
            # Parse +CMGL entries: +CMGL: index,"status","sender",...\r\nbody\r\n
            messages = re.findall(
                r'\+CMGL:\s*\d+,"[^"]*","([^"]*)".*?\r?\n(.+?)(?=\r?\n\+CMGL:|\r?\nOK|\Z)',
                resp, re.DOTALL
            )
            for sender, body in messages:
                self.log_sms(sender, body.strip())
            if messages:
                logging.info(f"[{self.carrier}] SMS: saved {len(messages)} message(s) to message.log")
        except Exception as e:
            logging.error(f"[{self.carrier}] SMS: error saving messages | {e}")

    def delete_all_sms(self):
        """Save all SMS to message.log, then delete them from SIM."""
        try:
            self.send_at('AT+CMGF=1', 1)
            self._save_all_sms()
            resp = self.send_at('AT+CMGDA="DEL ALL"', 3)
            if "OK" in resp:
                logging.info(f"[{self.carrier}] SMS: all messages deleted")
            else:
                logging.warning(f"[{self.carrier}] SMS: delete failed | {resp}")
        except Exception as e:
            logging.error(f"[{self.carrier}] SMS: cleanup error | {e}")

    # =====================
    # SIGNAL CHECK
    # =====================

    def get_signal(self):
        """Return signal strength (0-31, 99=unknown). 0=no signal."""
        try:
            resp = self.send_at("AT+CSQ", 1)
            if "+CSQ:" in resp:
                csq_line = resp.split("+CSQ:")[1].split("\n")[0].strip()
                parts = csq_line.split(",")
                return int(parts[0].strip())
        except:
            pass
        return 0

    def check_registration(self):
        """Check network registration. Returns (registered: bool, stat: int).
        CREG stat: 0=not searching, 1=home, 2=searching, 3=denied, 5=roaming.
        Only 1 (home) and 5 (roaming) mean the modem can make USSD calls."""
        try:
            resp = self.send_at("AT+CREG?", 1)
            if "+CREG:" in resp:
                creg_line = resp.split("+CREG:")[1].split("\n")[0].strip()
                parts = creg_line.split(",")
                stat = int(parts[1].strip()) if len(parts) > 1 else int(parts[0].strip())
                registered = stat in (1, 5)
                if not registered:
                    logging.warning(f"[{self.carrier}] CREG: not registered (stat={stat})")
                return registered, stat
        except Exception as e:
            logging.error(f"[{self.carrier}] CREG: check error | {e}")
        return False, -1

    def check_balance(self):
        """Check SIM balance via USSD. The response arrives as an SMS.
        Returns balance as float or None."""
        try:
            # Set text mode — wait for OK dynamically instead of fixed 1s sleep
            self._send_raw_and_wait_ok('AT+CMGF=1')

            # Send USSD command — flush first, then write directly.
            # Do NOT use send_at() here: its fixed sleep would consume and
            # discard the async USSD/SMS response from the serial buffer.
            self._flush_serial()
            self.ser.write((f'AT+CUSD=1,"{self.balance_ussd}",15\r').encode())

            # Poll for +CMTI (SMS notification) or +CUSD with balance data
            start = time.time()
            collected = ""
            sms_index = None
            while time.time() - start < 20:
                if self.ser.in_waiting:
                    chunk = self.ser.read(self.ser.in_waiting).decode(errors="ignore")
                    collected += chunk

                    # Check if SMS arrived: +CMTI: "SM",<index>
                    cmti_match = re.search(r'\+CMTI:\s*"SM"\s*,\s*(\d+)', collected)
                    if cmti_match:
                        sms_index = cmti_match.group(1)
                        break

                    # Some carriers return balance directly in CUSD
                    if "+CUSD:" in collected and re.search(r'\d+[.,]?\d*\s*(?:DH|MAD|dh|mad)', collected):
                        break
                time.sleep(0.2)

            logging.info(f"[{self.carrier}] BALANCE RAW: {collected}")

            # If we got an SMS notification, read that SMS
            sms_body = ""
            if sms_index is not None:
                resp = self._send_raw_and_wait_ok(f'AT+CMGR={sms_index}', timeout=3)
                logging.info(f"[{self.carrier}] BALANCE SMS: {resp}")

                # Extract SMS body (line after +CMGR: header)
                body_match = re.search(r'\+CMGR:.*?\r?\n(.+?)(?:\r?\nOK|\Z)', resp, re.DOTALL)
                if body_match:
                    sms_body = body_match.group(1).strip()

                # Log and delete the SMS
                self.log_sms("BALANCE_SMS", sms_body or resp)
                self._send_raw_and_wait_ok(f'AT+CMGD={sms_index}', timeout=2)

            text_to_parse = sms_body or collected

            # Log USSD response if no SMS was read
            if not sms_body and collected.strip():
                self.log_sms("BALANCE_USSD", collected.strip())

            # Parse balance from response
            match = re.search(r'(\d+[.,]\d+)\s*(?:DH|MAD|dh|mad)', text_to_parse)
            if match:
                balance = float(match.group(1).replace(',', '.'))
                self.cfg["sim_balance"] = balance
                logging.info(f"[{self.carrier}] BALANCE: {balance} MAD")
                return balance

            # Try integer pattern: "12 DH"
            match = re.search(r'(\d+)\s*(?:DH|MAD|dh|mad)', text_to_parse)
            if match:
                balance = float(match.group(1))
                self.cfg["sim_balance"] = balance
                logging.info(f"[{self.carrier}] BALANCE: {balance} MAD")
                return balance

            logging.warning(f"[{self.carrier}] BALANCE: could not parse | {text_to_parse}")
            return None
        except Exception as e:
            logging.error(f"[{self.carrier}] BALANCE: error | {e}")
            return None

    def has_signal(self):
        """Check if modem has minimum usable signal AND is registered on network."""
        sig = self.get_signal()
        sig_ok = sig >= config.MIN_SIGNAL and sig != 99
        if not sig_ok:
            logging.warning(f"[{self.carrier}] SIGNAL: too low ({sig}/31, min={config.MIN_SIGNAL})")
            return False

        registered, stat = self.check_registration()
        if not registered:
            logging.warning(f"[{self.carrier}] SIGNAL: ok ({sig}/31) but not registered (CREG stat={stat})")
            return False

        return True

    # =====================
    # MODEM CHECK & RECOVERY
    # =====================

    def modem_check(self):
        """Check if modem is alive and responding."""
        try:
            r = self.send_at("AT", 1)
            return "OK" in r
        except:
            return False

    def force_register(self):
        """Force the modem to search and register on the network."""
        logging.info(f"[{self.carrier}] MODEM: forcing network registration...")
        try:
            self.send_at("AT+COPS=0", 5)
            time.sleep(5)
            registered, stat = self.check_registration()
            if registered:
                logging.info(f"[{self.carrier}] MODEM: network registration recovered (CREG={stat})")
                return True
            logging.warning(f"[{self.carrier}] MODEM: AT+COPS=0 didn't help (CREG={stat}), doing full reset")
            return self.modem_reset()
        except Exception as e:
            logging.error(f"[{self.carrier}] MODEM: force_register error | {e}")
            return False

    def modem_reset(self):
        """Attempt to reset the modem via AT command."""
        logging.warning(f"[{self.carrier}] MODEM: attempting reset...")
        try:
            self.send_at("AT+CFUN=1,1", 5)
            time.sleep(10)
            if self.modem_check():
                logging.info(f"[{self.carrier}] MODEM: reset successful")
                self.cfg["modem_ok"] = True
                self.delete_all_sms()
                return True
            else:
                logging.error(f"[{self.carrier}] MODEM: reset failed, still not responding")
                self.cfg["modem_ok"] = False
                return False
        except:
            logging.error(f"[{self.carrier}] MODEM: reset error")
            self.cfg["modem_ok"] = False
            return False

    def _update_health_cache(self, modem_alive, signal, registered, creg_stat):
        """Update cached health values used by /health and dashboard endpoints."""
        self.cfg["modem_ok"] = bool(modem_alive)
        self.cfg["last_signal"] = int(signal) if signal is not None else -1
        self.cfg["last_registered"] = bool(registered)
        self.cfg["last_creg_stat"] = int(creg_stat) if creg_stat is not None else -1
        self.cfg["last_health_check_ts"] = time.time()

    def _send_telegram_message(self, text):
        """Send a Telegram message using bot token + chat id from config."""
        token = config.TELEGRAM_BOT_TOKEN
        if not token:
            return False

        chat_id = self._resolve_telegram_chat_id()
        if not chat_id:
            logging.warning(
                f"[{self.carrier}] MODEM ALERT: no Telegram chat id found. "
                f"Send /start to the bot once or set TELEGRAM_CHAT_ID."
            )
            return False

        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = urllib.parse.urlencode({
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": "true",
        }).encode("utf-8")

        req = urllib.request.Request(url, data=payload, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return 200 <= int(resp.getcode()) < 300
        except Exception as e:
            logging.error(f"[{self.carrier}] MODEM ALERT: Telegram send failed | {e}")
            return False

    def _discover_telegram_chat_id(self):
        """Try to discover the latest chat id from Telegram getUpdates."""
        token = config.TELEGRAM_BOT_TOKEN
        if not token:
            return ""

        url = f"https://api.telegram.org/bot{token}/getUpdates?limit=20"
        try:
            with urllib.request.urlopen(url, timeout=10) as resp:
                payload = resp.read().decode("utf-8", errors="ignore")
                data = json.loads(payload)
        except Exception as e:
            logging.error(f"[{self.carrier}] MODEM ALERT: getUpdates failed | {e}")
            return ""

        if not data.get("ok"):
            return ""

        updates = data.get("result", [])
        for upd in reversed(updates):
            msg = upd.get("message") or upd.get("channel_post") or upd.get("edited_message")
            if not isinstance(msg, dict):
                continue
            chat = msg.get("chat") or {}
            chat_id = chat.get("id")
            if chat_id is not None:
                return str(chat_id)
        return ""

    def _resolve_telegram_chat_id(self):
        """Resolve chat id from env, cache, or Telegram updates fallback."""
        chat_id = (config.TELEGRAM_CHAT_ID or "").strip()
        if chat_id:
            return chat_id

        cached = str(self.cfg.get("telegram_chat_id_cache", "") or "").strip()
        if cached:
            return cached

        discovered = self._discover_telegram_chat_id()
        if discovered:
            self.cfg["telegram_chat_id_cache"] = discovered
            logging.info(f"[{self.carrier}] MODEM ALERT: discovered Telegram chat id {discovered}")
            return discovered
        return ""

    def _handle_offline_alert(self):
        """Alert once when modem/network stays offline beyond configured threshold."""
        now = time.time()
        modem_ok = bool(self.cfg.get("modem_ok", False))
        registered = bool(self.cfg.get("last_registered", False))
        offline = (not modem_ok) or (not registered)

        if not offline:
            self.cfg["offline_since_ts"] = 0.0
            self.cfg["offline_alert_sent"] = False
            self.cfg["offline_alert_last_try_ts"] = 0.0
            return

        offline_since = float(self.cfg.get("offline_since_ts", 0) or 0)
        if offline_since <= 0:
            self.cfg["offline_since_ts"] = now
            return

        threshold_sec = max(60, int(config.MODEM_OFFLINE_ALERT_SEC))
        offline_for = now - offline_since
        if offline_for < threshold_sec:
            return

        if bool(self.cfg.get("offline_alert_sent", False)):
            return

        retry_sec = max(30, int(config.MODEM_OFFLINE_ALERT_RETRY_SEC))
        last_try = float(self.cfg.get("offline_alert_last_try_ts", 0) or 0)
        if (now - last_try) < retry_sec:
            return
        self.cfg["offline_alert_last_try_ts"] = now

        if not config.TELEGRAM_BOT_TOKEN:
            logging.warning(
                f"[{self.carrier}] MODEM ALERT: Telegram not configured "
                f"(needs TELEGRAM_BOT_TOKEN)"
            )
            return

        signal = int(self.cfg.get("last_signal", -1))
        creg_stat = int(self.cfg.get("last_creg_stat", -1))
        text = (
            f"Gateway alert: modem {self.carrier.upper()} offline for {int(offline_for)}s. "
            f"modem_ok={modem_ok}, registered={registered}, signal={signal}, CREG={creg_stat}."
        )
        if self._send_telegram_message(text):
            self.cfg["offline_alert_sent"] = True
            logging.warning(f"[{self.carrier}] MODEM ALERT: Telegram sent (offline {int(offline_for)}s)")

    def modem_health_monitor(self):
        """Background thread: check modem health every 60s. Auto-recover.
        Balance is checked ONLY after each recharge (in the worker), not here.
        SKIPS entirely when a recharge is in progress to avoid modem interference."""
        interval = max(10, int(config.HEALTH_CHECK_INTERVAL_SEC))
        time.sleep(interval)  # Initial delay (modem was just checked at startup)
        while True:
            # Fast-path: skip without even trying to acquire the lock
            if self.cfg["recharge_in_progress"]:
                logging.info(f"[{self.carrier}] MODEM HEALTH: skipped — recharge in progress")
                self._handle_offline_alert()
                time.sleep(interval)
                continue

            acquired = self.serial_lock.acquire(timeout=5)
            if not acquired:
                self._handle_offline_alert()
                time.sleep(interval)
                continue
            try:
                # Double-check INSIDE lock
                if self.cfg["recharge_in_progress"]:
                    logging.info(f"[{self.carrier}] MODEM HEALTH: skipped — recharge started while waiting for lock")
                else:
                    if self.modem_check():
                        signal = self.get_signal()
                        registered, stat = self.check_registration()
                        self._update_health_cache(True, signal, registered, stat)
                        if not registered:
                            logging.warning(f"[{self.carrier}] MODEM: alive but not registered (CREG={stat})")
                            if stat in (0, 3):
                                self.force_register()
                    else:
                        logging.error(f"[{self.carrier}] MODEM: health check failed")
                        self._update_health_cache(False, -1, False, -1)
                        self.modem_reset()
            except Exception as e:
                logging.error(f"[{self.carrier}] MODEM: health monitor error | {e}")
                self._update_health_cache(False, -1, False, -1)
            finally:
                self.serial_lock.release()

            self._handle_offline_alert()
            time.sleep(interval)

    # =====================
    # SMS PARSER
    # =====================

    def _classify_sms_status(self, text):
        """Classify recharge result from an SMS body.

        Returns one of: success, rejected, balance_error, or None when unknown.
        """
        low = text.lower()

        # Balance error (French + Arabic)
        if any(kw in low for kw in ["insuffisant", "solde insuffisant"]) or \
           any(kw in text for kw in ["رصيد غير كافي", "غير كافي", "الرصيد غير كاف"]):
            return "balance_error"

        # Success (French + Arabic)
        if any(kw in low for kw in [
            "effectuee", "effectue", "succes", "success", "credite", "recharge a ete",
            "a ete recharge", "a ete recharge de", "solde restant est",
            "votre solde recharge est de", "solde d'encaissement"
        ]) or \
           any(kw in text for kw in ["تمت", "بنجاح", "تم شحن", "تمت العملية", "تم التعبئة"]):
            return "success"

        # Rejected/invalid request (French + Arabic)
        if any(kw in low for kw in [
            "rejete", "refuse", "erreur", "echoue", "failure", "failed", "invalide", "incorrect",
            "inexistante", "numero compose est correct"
        ]) or any(kw in text for kw in ["مرفوض", "خطأ", "فشل", "غير صالح", "غير صحيح", "رفض"]):
            return "rejected"

        return None

    def read_sms(self, timeout=60, pre_collected=""):
        """Wait for an incoming SMS and parse the recharge result.
        Returns (status, raw_message) tuple."""
        start = time.time()
        collected = pre_collected or ""
        logging.info(f"[{self.carrier}] SMS: waiting for confirmation (timeout={timeout}s, pre={len(collected)} bytes)...")

        while time.time() - start < timeout:
            if self.ser.in_waiting:
                chunk = self.ser.read(self.ser.in_waiting).decode(errors="ignore")
                collected += chunk
                logging.info(f"[{self.carrier}] SMS RAW CHUNK: {repr(chunk)}")

                if "+CMT:" in collected:
                    time.sleep(2)
                    if self.ser.in_waiting:
                        extra = self.ser.read(self.ser.in_waiting).decode(errors="ignore")
                        collected += extra
                        logging.info(f"[{self.carrier}] SMS RAW EXTRA: {repr(extra)}")

                    # Parse all +CMT SMS blocks and classify from newest to oldest.
                    # This avoids using an older balance-only SMS as the final result.
                    sms_blocks = re.findall(
                        r'\+CMT:\s*"([^"]*)".*?\r?\n(.+?)(?=\r?\n\+CMT:|\Z)',
                        collected,
                        re.DOTALL,
                    )

                    if sms_blocks:
                        # Log latest block for visibility in message.log
                        latest_sender, latest_body = sms_blocks[-1]
                        latest_body = latest_body.strip()
                        self.log_sms(latest_sender or "Unknown", latest_body)

                        for sender, body in reversed(sms_blocks):
                            body = body.strip()
                            status = self._classify_sms_status(body)
                            if status:
                                return status, body

                        return "unknown", latest_body

                    # Fallback when +CMT exists but regex extraction fails
                    fallback_status = self._classify_sms_status(collected)
                    return fallback_status or "unknown", collected.strip()

            time.sleep(0.5)

        logging.warning(f"[{self.carrier}] SMS: timeout after {timeout}s — no confirmation received")
        logging.warning(f"[{self.carrier}] SMS: raw collected data: {repr(collected)}")
        return "unknown", "TIMEOUT: No SMS received"

    # =====================
    # RECHARGE (USSD)
    # =====================

    def _flush_serial(self):
        """Drain any leftover data from the serial buffer."""
        if self.ser.in_waiting:
            self.ser.read_all()

    def _send_raw(self, cmd, wait=0.5):
        """Send an AT command without reading response — avoids eating SMS data."""
        self.ser.write((cmd + "\r").encode())
        time.sleep(wait)

    def _send_raw_and_wait_ok(self, cmd, timeout=3):
        """Send an AT command and wait for OK/ERROR — reads ONLY the command response."""
        self.ser.write((cmd + "\r").encode())
        start = time.time()
        collected = ""
        while time.time() - start < timeout:
            if self.ser.in_waiting:
                chunk = self.ser.read(self.ser.in_waiting).decode(errors="ignore")
                collected += chunk
                if "OK" in collected or "ERROR" in collected:
                    break
            time.sleep(0.1)
        logging.info(f"[{self.carrier}] RAW_AT {cmd} | {collected.strip()}")
        return collected

    def recharge(self, phone, price, offer):
        """Execute a recharge via USSD. Returns (status, message) tuple.

        CRITICAL: After sending ATD, we NEVER call send_at() or read_all()
        until read_sms() has captured the confirmation."""
        code = self.recharge_code_template.format(phone=phone, price=price, offer=offer)

        with self.serial_lock:
            self.cfg["recharge_in_progress"] = True
            try:
                logging.info(f"[{self.carrier}] RECHARGE START: {phone} {price} MAD (offer={offer})")

                # 1. Pre-cleanup
                self.delete_all_sms()

                if not self.has_signal():
                    return "no_signal", "No signal or not registered on network"

                # 2. Configure SMS push mode + flush buffer
                self.send_at("AT+CMGF=1", 1)
                self.send_at("AT+CNMI=2,2,0,0,0", 1)
                self._flush_serial()

                # 3. Send the recharge command
                logging.info(f"[{self.carrier}] RECHARGE: sending USSD command: {code}")
                self.ser.write((f"ATD {code};\r").encode())

                # Wait for SIM800L to acknowledge the dial command
                time.sleep(1)
                atd_response = ""
                if self.ser.in_waiting:
                    atd_response = self.ser.read(self.ser.in_waiting).decode(errors="ignore")
                    if "+CMT:" in atd_response:
                        logging.info(f"[{self.carrier}] RECHARGE: SMS arrived during ATD response!")
                    else:
                        logging.info(f"[{self.carrier}] RECHARGE: ATD response: {atd_response.strip()}")
                        atd_response = ""

                # 4. Wait for confirmation SMS
                status, message = self.read_sms(timeout=60, pre_collected=atd_response)

                # 5. Hang up
                self.send_at("ATH", 1)

                logging.info(f"[{self.carrier}] RECHARGE FINISHED: {status} | {message}")

            finally:
                self.cfg["recharge_in_progress"] = False

        return status, message


# =====================
# SELF-TEST
# =====================

if __name__ == "__main__":
    import sys
    CREG_LABELS = {0: 'Not searching', 1: 'Home', 2: 'Searching', 3: 'Denied', 5: 'Roaming'}

    carrier = sys.argv[1] if len(sys.argv) > 1 else "orange"
    if carrier not in config.MODEMS:
        print(f"Unknown carrier: {carrier}. Available: {list(config.MODEMS.keys())}")
        sys.exit(1)

    cfg = config.MODEMS[carrier]
    m = Modem(carrier, cfg)

    print("=" * 40)
    print(f"  Modem Self-Test [{carrier.upper()}]")
    print(f"  Port: {cfg['serial_port']}")
    print("=" * 40)

    with cfg["serial_lock"]:
        alive = m.modem_check()
        print(f"  Modem alive : {'YES' if alive else 'NO'}")

        if alive:
            sig = m.get_signal()
            print(f"  Signal (CSQ): {sig}/31 {'OK' if sig >= config.MIN_SIGNAL else 'LOW'}")

            registered, stat = m.check_registration()
            label = CREG_LABELS.get(stat, f'Unknown({stat})')
            print(f"  Network CREG: {label} {'OK' if registered else 'NOT REGISTERED'}")

            m.delete_all_sms()
            print(f"  SMS cleanup : done")

    print("=" * 40)
    print(f"  Ready to recharge: {'YES' if alive and sig >= config.MIN_SIGNAL and registered else 'NO'}")
