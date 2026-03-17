#!/usr/bin/env python3
"""
Monitor /var/log/mail.log for outgoing emails (status=sent).
Sends a Telegram alert to all subscribers if more than THRESHOLD emails
are sent within WINDOW_SECONDS. Users subscribe via /subscribe bot command.
"""

import time
import re
import urllib.request
import urllib.parse
import json
import os
import sys
import logging
import threading
from collections import deque
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).parent


def load_env(path: str):
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


load_env(str(BASE_DIR / ".env"))

# --- Config ---
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ADMIN_CHAT_ID      = os.environ["TELEGRAM_CHAT_ID"]   # always receives alerts
MAIL_LOG           = "/var/log/mail.log"
THRESHOLD          = int(os.environ.get("THRESHOLD", 10))
WINDOW_SECONDS     = int(os.environ.get("WINDOW_SECONDS", 60))
COOLDOWN_SECONDS   = int(os.environ.get("COOLDOWN_SECONDS", 300))
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

def load_subscribers() -> set:
    if SUBSCRIBERS_FILE.exists():
        return set(json.loads(SUBSCRIBERS_FILE.read_text()))
    return {ADMIN_CHAT_ID}


def save_subscribers(subs: set):
    SUBSCRIBERS_FILE.write_text(json.dumps(list(subs)))


subscribers_lock = threading.Lock()
subscribers: set = load_subscribers()
# Ensure admin is always in the set
subscribers.add(ADMIN_CHAT_ID)
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


def send_to(chat_id: str, message: str) -> bool:
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


def broadcast(message: str):
    with subscribers_lock:
        targets = set(subscribers)
    for chat_id in targets:
        send_to(chat_id, message)

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
                if not chat_id:
                    continue

                if text == "/subscribe":
                    with subscribers_lock:
                        already = chat_id in subscribers
                        subscribers.add(chat_id)
                        save_subscribers(subscribers)
                    if already:
                        send_to(chat_id, "You are already subscribed to email alerts.")
                    else:
                        log.info(f"New subscriber: {chat_id}")
                        send_to(chat_id, "✅ <b>Subscribed!</b>\nYou will now receive email volume alerts.")

                elif text == "/unsubscribe":
                    with subscribers_lock:
                        was_in = chat_id in subscribers
                        subscribers.discard(chat_id)
                        save_subscribers(subscribers)
                    if was_in:
                        log.info(f"Unsubscribed: {chat_id}")
                        send_to(chat_id, "🔕 <b>Unsubscribed.</b>\nYou will no longer receive alerts.")
                    else:
                        send_to(chat_id, "You were not subscribed.")

                elif text == "/status":
                    with subscribers_lock:
                        count = len(subscribers)
                    send_to(chat_id, f"📊 <b>Postwatch status</b>\n"
                                     f"Threshold: {THRESHOLD} emails / {WINDOW_SECONDS}s\n"
                                     f"Subscribers: {count}")

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
    log.info(f"Email monitor started. Threshold: {THRESHOLD} emails/{WINDOW_SECONDS}s. Log: {MAIL_LOG}")

    # Start bot polling thread
    t = threading.Thread(target=poll_bot, daemon=True)
    t.start()

    broadcast("✅ <b>Postwatch started</b>\nWill alert if more than "
              f"{THRESHOLD} emails are sent within {WINDOW_SECONDS} seconds.\n\n"
              "Commands: /subscribe, /unsubscribe, /status")

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
                msg = (
                    f"⚠️ <b>High email volume alert</b>\n"
                    f"<b>{count}</b> emails sent in the last {WINDOW_SECONDS} seconds "
                    f"(threshold: {THRESHOLD})\n"
                    f"🕐 {wall_now}"
                )
                log.warning(f"ALERT: {count} emails in last {WINDOW_SECONDS}s — sending Telegram notification")
                broadcast(msg)
                log.info("Telegram notifications sent")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("Stopped by user")
    except Exception as e:
        log.critical(f"Unexpected error: {e}", exc_info=True)
        sys.exit(1)
