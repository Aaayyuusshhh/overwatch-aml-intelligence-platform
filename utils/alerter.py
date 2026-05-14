"""
Push notifications for failures and layout changes.

Per TECH_STACK §6.1 (Telegram preferred, urllib only — no new deps) and
ARCHITECTURE.md §3.5. Email path is a stub for Phase 1.

Behaviour:
  - If TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID env vars are both set:
    POST to api.telegram.org/bot<token>/sendMessage via stdlib urllib.
  - Otherwise: print 'ALERT [<severity>]: <message>' to stderr.
  - If a Telegram POST fails, fall through to the stderr branch so the
    operator still sees the alert.
"""

import json
import os
import sys
import urllib.parse
import urllib.request

TELEGRAM_API = "https://api.telegram.org"


def _stderr(message, severity):
    print(f"ALERT [{severity}]: {message}", file=sys.stderr)


def _send_telegram(token, chat_id, message, severity):
    url = f"{TELEGRAM_API}/bot{token}/sendMessage"
    payload = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": f"[{severity.upper()}] {message}",
    }).encode("utf-8")
    req = urllib.request.Request(url, data=payload, method="POST")
    with urllib.request.urlopen(req, timeout=10) as resp:
        resp.read()


def send_alert(message, severity="info"):
    """Send a one-shot alert. Always succeeds (falls back to stderr)."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    if token and chat_id:
        try:
            _send_telegram(token, chat_id, message, severity)
            return
        except Exception as e:
            _stderr(f"{message}  (telegram failed: {e})", severity)
            return
    _stderr(message, severity)


def send_email_alert(subject, body):  # noqa: ARG001
    """TODO Phase 3: SMTP fallback via stdlib smtplib. Not used in Phase 1."""
    return None
