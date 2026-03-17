#!/usr/bin/env python3
"""
Monitor /var/log/mail.log for outgoing emails (status=sent).
Sends alerts to Telegram subscribers and/or email addresses when the
volume exceeds THRESHOLD within WINDOW_SECONDS.

Telegram bot commands:
  /subscribe                - subscribe current chat to alerts
  /unsubscribe              - unsubscribe current chat
  /subscribe user@example.com   - add an email address to alerts
  /unsubscribe user@example.com - remove an email address from alerts
  /status                   - show settings and subscriber counts
  /list                     - show all subscribers (admin only)
"""

import time
import re
import smtplib
import urllib.request
import urllib.parse
import json
import os
import sys
import logging
import threading
from collections import deque
from datetime import datetime
from email.mime.text import MIMEText
from pathlib import Path

BASE_DIR = Path(__file__).parent
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def load_env(path: str):
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


load_env(str(BASE_DIR / ".env"))

# --- Config ---
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ADMIN_CHAT_ID      = os.environ["TELEGRAM_CHAT_ID"]
MAIL_LOG           = "/var/log/mail.log"
THRESHOLD          = int(os.environ.get("THRESHOLD", 10))
WINDOW_SECONDS     = int(os.environ.get("WINDOW_SECONDS", 60))
COOLDOWN_SECONDS   = int(os.environ.get("COOLDOWN_SECONDS", 300))
ALERT_FROM_EMAIL   = os.environ.get("ALERT_FROM_EMAIL", f"postwatch@{os.uname().nodename}")
SMTP_HOST          = os.environ.get("SMTP_HOST", "localhost")
SMTP_PORT          = int(os.environ.get("SMTP_PORT", 25))
SUBSCRIBERS_FILE   = BASE_DIR / "subscribers.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("/var/log/email_monitor.log"),
    ],
)
log = logging.getLogger(__name__)

SENT_RE = re.compile(r"status=sent")

# --- Subscriber store ---

def load_subscribers() -> dict:
    if SUBSCRIBERS_FILE.exists():
        data = json.loads(SUBSCRIBERS_FILE.read_text())
        # migrate old flat list format
        if isinstance(data, list):
            return {"telegram": data, "email": []}
        return data
    return {"telegram": [ADMIN_CHAT_ID], "email": []}


def save_subscribers(subs: dict):
    SUBSCRIBERS_FILE.write_text(json.dumps(subs, indent=2))


subscribers_lock = threading.Lock()
subscribers: dict = load_subscribers()
if ADMIN_CHAT_ID not in subscribers["telegram"]:
    subscribers["telegram"].append(ADMIN_CHAT_ID)
save_subscribers(subscribers)

# --- Telegram helpers ---

def tg_request(method: str, payload: dict) -> dict:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def send_telegram(chat_id: str, message: str) -> bool:
    try:
        result = tg_request("sendMessage", {
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "HTML",
        })
        return result.get("ok", False)
    except Exception as e:
        log.error(f"Telegram send to {chat_id} failed: {e}")
        return False


def send_email_alert(to_addr: str, subject: str, body: str) -> bool:
    try:
        msg = MIMEText(body, "plain")
        msg["Subject"] = subject
        msg["From"] = ALERT_FROM_EMAIL
        msg["To"] = to_addr
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as smtp:
            smtp.sendmail(ALERT_FROM_EMAIL, [to_addr], msg.as_string())
        return True
    except Exception as e:
        log.error(f"Email send to {to_addr} failed: {e}")
        return False


def broadcast(tg_message: str, email_subject: str, email_body: str):
    with subscribers_lock:
        tg_targets = list(subscribers["telegram"])
        email_targets = list(subscribers["email"])
    for chat_id in tg_targets:
        send_telegram(chat_id, tg_message)
    for addr in email_targets:
        send_email_alert(addr, email_subject, email_body)

# --- Bot command polling ---

def poll_bot():
    offset = 0
    while True:
        try:
            result = tg_request("getUpdates", {"offset": offset, "timeout": 30})
            for update in result.get("result", []):
                offset = update["update_id"] + 1
                message = update.get("message", {})
                text = message.get("text", "").strip()
                chat_id = str(message.get("chat", {}).get("id", ""))
                if not chat_id or not text:
                    continue

                parts = text.split(None, 1)
                command = parts[0].lower()
                arg = parts[1].strip() if len(parts) > 1 else ""

                if command == "/subscribe":
                    if arg and EMAIL_RE.match(arg):
                        # add email address
                        with subscribers_lock:
                            already = arg in subscribers["email"]
                            if not already:
                                subscribers["email"].append(arg)
                                save_subscribers(subscribers)
                        if already:
                            send_telegram(chat_id, f"📧 <b>{arg}</b> is already subscribed.")
                        else:
                            log.info(f"Email subscriber added: {arg} (by chat {chat_id})")
                            send_telegram(chat_id, f"✅ <b>{arg}</b> added to email alerts.")
                    elif arg:
                        send_telegram(chat_id, "❌ Invalid email address.")
                    else:
                        # subscribe Telegram chat
                        with subscribers_lock:
                            already = chat_id in subscribers["telegram"]
                            if not already:
                                subscribers["telegram"].append(chat_id)
                                save_subscribers(subscribers)
                        if already:
                            send_telegram(chat_id, "You are already subscribed to alerts.")
                        else:
                            log.info(f"Telegram subscriber added: {chat_id}")
                            send_telegram(chat_id, "✅ <b>Subscribed!</b>\nYou will now receive email volume alerts.")

                elif command == "/unsubscribe":
                    if arg and EMAIL_RE.match(arg):
                        # remove email address
                        with subscribers_lock:
                            was_in = arg in subscribers["email"]
                            if was_in:
                                subscribers["email"].remove(arg)
                                save_subscribers(subscribers)
                        if was_in:
                            log.info(f"Email subscriber removed: {arg} (by chat {chat_id})")
                            send_telegram(chat_id, f"🔕 <b>{arg}</b> removed from email alerts.")
                        else:
                            send_telegram(chat_id, f"📧 <b>{arg}</b> was not subscribed.")
                    elif arg:
                        send_telegram(chat_id, "❌ Invalid email address.")
                    else:
                        # unsubscribe Telegram chat
                        if chat_id == ADMIN_CHAT_ID:
                            send_telegram(chat_id, "⚠️ Admin cannot unsubscribe.")
                        else:
                            with subscribers_lock:
                                was_in = chat_id in subscribers["telegram"]
                                subscribers["telegram"].discard(chat_id) if hasattr(subscribers["telegram"], "discard") else (subscribers["telegram"].remove(chat_id) if was_in else None)
                                if was_in:
                                    save_subscribers(subscribers)
                            if was_in:
                                log.info(f"Telegram subscriber removed: {chat_id}")
                                send_telegram(chat_id, "🔕 <b>Unsubscribed.</b>\nYou will no longer receive alerts.")
                            else:
                                send_telegram(chat_id, "You were not subscribed.")

                elif command == "/status":
                    with subscribers_lock:
                        tg_count = len(subscribers["telegram"])
                        em_count = len(subscribers["email"])
                    send_telegram(chat_id,
                        f"📊 <b>Postwatch status</b>\n"
                        f"Threshold: {THRESHOLD} emails / {WINDOW_SECONDS}s\n"
                        f"Cooldown: {COOLDOWN_SECONDS}s\n"
                        f"Telegram subscribers: {tg_count}\n"
                        f"Email subscribers: {em_count}")

                elif command == "/list":
                    if chat_id != ADMIN_CHAT_ID:
                        send_telegram(chat_id, "⛔ Admin only.")
                        continue
                    with subscribers_lock:
                        tg_list = "\n".join(subscribers["telegram"]) or "none"
                        em_list = "\n".join(subscribers["email"]) or "none"
                    send_telegram(chat_id,
                        f"📋 <b>Subscribers</b>\n\n"
                        f"<b>Telegram:</b>\n{tg_list}\n\n"
                        f"<b>Email:</b>\n{em_list}")

        except Exception as e:
            log.error(f"Bot poll error: {e}")
            time.sleep(5)


# --- Mail log monitor ---

def tail_file(path: str):
    with open(path, "r") as f:
        f.seek(0, 2)
        while True:
            line = f.readline()
            if line:
                yield line
            else:
                time.sleep(0.2)


def main():
    log.info(f"Email monitor started. Threshold: {THRESHOLD} emails/{WINDOW_SECONDS}s.")

    t = threading.Thread(target=poll_bot, daemon=True)
    t.start()

    tg_msg = (
        "✅ <b>Postwatch started</b>\n"
        f"Alert threshold: {THRESHOLD} emails / {WINDOW_SECONDS}s\n\n"
        "Commands:\n"
        "/subscribe — receive Telegram alerts\n"
        "/subscribe user@example.com — add email alert\n"
        "/unsubscribe — stop Telegram alerts\n"
        "/unsubscribe user@example.com — remove email alert\n"
        "/status — show settings\n"
        "/list — list all subscribers (admin)"
    )
    broadcast(tg_msg, "Postwatch started", f"Postwatch monitoring started.\nThreshold: {THRESHOLD} emails/{WINDOW_SECONDS}s.")

    timestamps: deque = deque()
    last_alert_time: float = 0.0

    for line in tail_file(MAIL_LOG):
        if not SENT_RE.search(line):
            continue

        now = time.monotonic()
        wall_now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        timestamps.append(now)
        while timestamps and timestamps[0] < now - WINDOW_SECONDS:
            timestamps.popleft()

        count = len(timestamps)

        if count > THRESHOLD:
            if now - last_alert_time >= COOLDOWN_SECONDS:
                last_alert_time = now
                tg_msg = (
                    f"⚠️ <b>High email volume alert</b>\n"
                    f"<b>{count}</b> emails sent in the last {WINDOW_SECONDS} seconds "
                    f"(threshold: {THRESHOLD})\n"
                    f"🕐 {wall_now}"
                )
                email_subject = f"[postwatch] High email volume: {count} emails in {WINDOW_SECONDS}s"
                email_body = (
                    f"High email volume detected on {os.uname().nodename}\n\n"
                    f"{count} emails sent in the last {WINDOW_SECONDS} seconds (threshold: {THRESHOLD})\n"
                    f"Time: {wall_now}"
                )
                log.warning(f"ALERT: {count} emails in last {WINDOW_SECONDS}s")
                broadcast(tg_msg, email_subject, email_body)
                log.info("Notifications sent to all subscribers")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("Stopped by user")
    except Exception as e:
        log.critical(f"Unexpected error: {e}", exc_info=True)
        sys.exit(1)
