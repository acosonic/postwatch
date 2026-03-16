#!/usr/bin/env python3
"""
Monitor /var/log/mail.log for outgoing emails (status=sent).
Sends a Telegram alert if more than 10 emails are sent within any 60-second window.
"""

import time
import re
import urllib.request
import urllib.parse
import json
import os
import sys
import logging
from collections import deque
from datetime import datetime
from pathlib import Path


def load_env(path: str):
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


load_env(os.path.join(os.path.dirname(__file__), ".env"))

# --- Config ---
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
MAIL_LOG = "/var/log/mail.log"
THRESHOLD = int(os.environ.get("THRESHOLD", 10))
WINDOW_SECONDS = int(os.environ.get("WINDOW_SECONDS", 60))
COOLDOWN_SECONDS = int(os.environ.get("COOLDOWN_SECONDS", 300))

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


def send_telegram(message: str) -> bool:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
    }).encode()
    try:
        req = urllib.request.Request(url, data=data, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
            return result.get("ok", False)
    except Exception as e:
        log.error(f"Telegram send failed: {e}")
        return False


def tail_file(path: str):
    """Open file, seek to end, then yield new lines as they arrive."""
    with open(path, "r") as f:
        f.seek(0, 2)  # seek to end
        while True:
            line = f.readline()
            if line:
                yield line
            else:
                time.sleep(0.2)


def main():
    log.info(f"Email monitor started. Threshold: {THRESHOLD} emails/{WINDOW_SECONDS}s. Log: {MAIL_LOG}")

    # Send startup notification
    send_telegram("✅ <b>Email monitor started</b>\nWill alert if more than "
                  f"{THRESHOLD} emails are sent within {WINDOW_SECONDS} seconds.")

    timestamps: deque = deque()   # sliding window of sent-email timestamps
    last_alert_time: float = 0.0

    for line in tail_file(MAIL_LOG):
        if not SENT_RE.search(line):
            continue

        now = time.monotonic()
        wall_now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # Add this email to the window
        timestamps.append(now)

        # Drop entries outside the sliding window
        while timestamps and timestamps[0] < now - WINDOW_SECONDS:
            timestamps.popleft()

        count = len(timestamps)
        log.debug(f"status=sent detected | window count: {count}")

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
                if send_telegram(msg):
                    log.info("Telegram notification sent successfully")
                else:
                    log.error("Failed to send Telegram notification")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("Stopped by user")
    except Exception as e:
        log.critical(f"Unexpected error: {e}", exc_info=True)
        sys.exit(1)
