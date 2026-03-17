# postwatch

Monitors Postfix email sending rate and sends a Telegram alert when the volume exceeds a configurable threshold within a sliding time window.

## How it works

- Tails `/var/log/mail.log` in real-time
- Counts lines containing `status=sent` (one per delivered email) using a sliding window
- Fires a Telegram alert when the count exceeds `THRESHOLD` within `WINDOW_SECONDS`
- Repeats alerts at most once every `COOLDOWN_SECONDS` to avoid notification spam
- Sends a startup confirmation message on service start
- Supports multiple subscribers — users self-register via bot commands
- Subscriber list is persisted in `subscribers.json`

## Bot commands

| Command | Description |
|---------|-------------|
| `/subscribe` | Start receiving Telegram alerts |
| `/unsubscribe` | Stop receiving Telegram alerts |
| `/subscribe user@example.com` | Add an email address to alerts |
| `/unsubscribe user@example.com` | Remove an email address from alerts |
| `/status` | Show threshold settings and subscriber counts |
| `/list` | List all subscribers (admin only) |

The admin chat ID set in `TELEGRAM_CHAT_ID` always receives alerts and cannot unsubscribe.

Email addresses can also be added directly to `subscribers.json`:
```json
{
  "telegram": ["123456789"],
  "email": ["ops@example.com", "admin@example.com"]
}
```

## Example

![Telegram alert example](2026-03-16_17-37.png)

## Requirements

- Linux with Postfix
- Python 3.6+
- A Telegram bot (see setup instructions below)

## Creating a Telegram bot

1. Open Telegram and search for **@BotFather**
2. Send `/newbot`
3. Choose a **name** for your bot (e.g. "Postwatch Alerts")
4. Choose a **username** for your bot (must end in `bot`, e.g. `postwatch_alerts_bot`)
5. BotFather will reply with your **bot token** — save it for `TELEGRAM_BOT_TOKEN`
6. To get your **chat ID**:
   - Send any message to your new bot
   - Open `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` in a browser
   - Look for `"chat":{"id":123456789}` in the response — that number is your `TELEGRAM_CHAT_ID`

## Installation

1. **Clone the repository**
   ```bash
   git clone https://github.com/acosonic/postwatch.git /root/alerter
   cd /root/alerter
   ```

2. **Create the `.env` file**
   ```bash
   cp .env.example .env
   ```
   Edit `.env` and fill in your values:
   ```ini
   TELEGRAM_BOT_TOKEN=your_bot_token
   TELEGRAM_CHAT_ID=your_chat_id
   THRESHOLD=10
   WINDOW_SECONDS=60
   COOLDOWN_SECONDS=300
   ```

3. **Install the systemd service**
   ```bash
   cp postwatch.service /etc/systemd/system/email-monitor.service
   systemctl daemon-reload
   systemctl enable email-monitor
   systemctl start email-monitor
   ```

## Configuration

All settings are in `.env`:

| Variable | Default | Description |
|----------|---------|-------------|
| `TELEGRAM_BOT_TOKEN` | — | Bot token from @BotFather |
| `TELEGRAM_CHAT_ID` | — | Admin chat ID — always receives alerts, cannot unsubscribe |
| `THRESHOLD` | `10` | Max emails allowed within the window before alerting |
| `WINDOW_SECONDS` | `60` | Sliding window size in seconds |
| `COOLDOWN_SECONDS` | `300` | Minimum seconds between repeated alerts |
| `ALERT_FROM_EMAIL` | `postwatch@hostname` | From address for email alerts |
| `SMTP_HOST` | `localhost` | SMTP server for sending email alerts |
| `SMTP_PORT` | `25` | SMTP port |

After changing `.env`, restart the service:
```bash
systemctl restart email-monitor
```

## Useful commands

```bash
systemctl status email-monitor       # service status
journalctl -u email-monitor -f       # live logs
tail -f /var/log/email_monitor.log   # file log
```
