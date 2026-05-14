"""
Slack notifier for the AML Pipeline.

CTO-facing daily run summaries + immediate alerts for layout changes,
scraper failures, and first-time-completed sources.

Companion to utils/alerter.py (Telegram / stderr). The two are
independent: both can be enabled, neither is required, and a Slack
outage never blocks a pipeline run.

Configuration
-------------
Webhook URL is read from $SLACK_WEBHOOK_URL. If unset, every send_*
function logs a warning and returns False; the pipeline keeps going.
We do NOT raise — a misconfigured Slack must never crash an end-to-end
scrape that took an hour to run.

Constraints
-----------
- urllib only (no `requests`, no `slack_sdk` per TECH_STACK §6.1)
- 10s timeout on every Slack call
- All network calls wrapped in try/except; failures log "WARN: Slack
  notification failed: <error>" and return False
- Timestamps in IST (Asia/Kolkata, UTC+5:30)

Public API
----------
send_slack_message(blocks, fallback_text)              -> bool
send_daily_summary(stats_dict)                         -> bool
send_layout_change_alert(name, agency, url, old, new)  -> bool
send_error_alert(source_name, agency, error_msg)       -> bool
send_new_source_alert(source_name, agency, records,
                      method="HTML extraction")        -> bool
send_test_message()                                    -> bool
"""

import json
import os
import smtplib
import socket
import ssl
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone, timedelta
from email.message import EmailMessage

WEBHOOK_ENV = "SLACK_WEBHOOK_URL"
TIMEOUT = 10
EMAIL_TIMEOUT = 30
IST = timezone(timedelta(hours=5, minutes=30), name="IST")

EMAIL_ENV = (
    "EMAIL_SMTP_HOST", "EMAIL_SMTP_PORT",
    "EMAIL_SENDER", "EMAIL_PASSWORD", "EMAIL_RECIPIENT",
)


def _ist_now():
    return datetime.now(IST)


def _fmt_ts(dt=None):
    """'8 May 2026, 06:12 AM IST'"""
    dt = dt or _ist_now()
    # Strip leading zero on day to match the design (`8 May` not `08 May`).
    return dt.strftime("%-d %b %Y, %I:%M %p IST")


def _log(msg):
    print(f"[notifier] {msg}", file=sys.stderr)


# --------------------------------------------------------------------------
# Low-level transport
# --------------------------------------------------------------------------
def _post(payload):
    """POST `payload` (dict) to the configured webhook. Returns True on
    success, False on any failure (no exceptions propagate)."""
    url = os.environ.get(WEBHOOK_ENV)
    if not url:
        _log(f"WARN: ${WEBHOOK_ENV} not set; skipping Slack notification")
        return False
    try:
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url, data=body, method="POST",
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            resp.read()
        return True
    except urllib.error.HTTPError as e:
        _log(f"WARN: Slack notification failed: HTTPError {e.code} {e.reason}")
    except urllib.error.URLError as e:
        _log(f"WARN: Slack notification failed: URLError {e.reason}")
    except Exception as e:
        _log(f"WARN: Slack notification failed: {type(e).__name__}: {e}")
    return False


def send_slack_message(blocks, fallback_text):
    """Low-level: send a Block Kit payload with mobile fallback text."""
    payload = {"text": fallback_text, "blocks": blocks}
    return _post(payload)


# --------------------------------------------------------------------------
# Block Kit helpers
# --------------------------------------------------------------------------
def _section(text):
    return {"type": "section",
            "text": {"type": "mrkdwn", "text": text}}


def _divider():
    return {"type": "divider"}


def _header(text):
    return {"type": "header",
            "text": {"type": "plain_text", "text": text[:150], "emoji": True}}


def _context(text):
    return {"type": "context",
            "elements": [{"type": "mrkdwn", "text": text}]}


# --------------------------------------------------------------------------
# Function 2: send_daily_summary
# --------------------------------------------------------------------------
def send_daily_summary(stats):
    """Send the daily run report.

    `stats` keys (all optional with safe defaults):
        date              - "8 May 2026"
        total_sources     - int (default 244)
        completed         - int
        records_total     - int
        records_new       - int (signed; e.g. +47 or -3)
        agencies          - int (count of agencies with >=1 record)
        successful_runs   - int
        failed_runs       - int
        skipped_runs      - int
        layout_changes    - list of {"source": ..., "detail": ...}
        failures          - list of {"source": ..., "reason": ...}
    """
    date_str        = stats.get("date", _ist_now().strftime("%-d %b %Y"))
    total_sources   = stats.get("total_sources", 244)
    completed       = stats.get("completed", 0)
    records_total   = stats.get("records_total", 0)
    records_new     = stats.get("records_new", 0)
    agencies        = stats.get("agencies", 0)
    successful_runs = stats.get("successful_runs", 0)
    failed_runs     = stats.get("failed_runs", 0)
    skipped_runs    = stats.get("skipped_runs", 0)
    layout_changes  = stats.get("layout_changes") or []
    failures        = stats.get("failures") or []

    delta = f"+{records_new:,}" if records_new >= 0 else f"{records_new:,}"
    timestamp = _ist_now().strftime("%-d %b %Y, %I:%M %p IST")

    blocks = [
        _header("AML Pipeline — Daily Run Report"),
        _section(f":calendar: *Date:* {date_str} ({timestamp})"),
        _divider(),
        _section(
            ":chart_with_upwards_trend: *Pipeline Metrics*\n"
            f"• Sources Completed: `{completed} / {total_sources}`\n"
            f"• Records in DB: `{records_total:,}`\n"
            f"• New Records Today: `{delta}`\n"
            f"• Agencies Covered: `{agencies}`"
        ),
        _section(
            ":gear: *Run Status*\n"
            f"• Successful: `{successful_runs}`\n"
            f"• Failed: `{failed_runs}`\n"
            f"• Skipped: `{skipped_runs}`"
        ),
    ]

    if layout_changes:
        lines = "\n".join(
            f"• *{lc.get('source', '?')}* — {lc.get('detail', 'hash changed')}"
            for lc in layout_changes[:10])
        if len(layout_changes) > 10:
            lines += f"\n• … +{len(layout_changes) - 10} more"
        blocks.append(_section(
            f":warning: *Layout Changes Detected ({len(layout_changes)})*\n{lines}"))

    if failures:
        lines = "\n".join(
            f"• *{f.get('source', '?')}* — `{(f.get('reason') or '')[:120]}`"
            for f in failures[:10])
        if len(failures) > 10:
            lines += f"\n• … +{len(failures) - 10} more"
        blocks.append(_section(
            f":x: *Failures ({len(failures)})*\n{lines}"))

    if not layout_changes and not failures:
        blocks.append(_section(
            ":white_check_mark: No layout changes, no failures."))

    blocks.append(_divider())

    fallback = (f"AML Pipeline — {date_str} — completed={completed}/{total_sources}, "
                f"records={records_total:,} ({delta}), "
                f"failures={len(failures)}, layout_changes={len(layout_changes)}")
    return send_slack_message(blocks, fallback)


# --------------------------------------------------------------------------
# Function 3: send_layout_change_alert
# --------------------------------------------------------------------------
def send_layout_change_alert(source_name, agency, url, old_hash, new_hash):
    blocks = [
        _section(":warning: *Layout Change Detected*"),
        _section(
            f"*Source:* {source_name}\n"
            f"*Agency:* {agency}\n"
            f"*URL:* <{url}|open>\n"
            f"*Previous Hash:* `{(old_hash or '?')[:12]}`\n"
            f"*New Hash:* `{(new_hash or '?')[:12]}`\n"
            f"*Time:* {_fmt_ts()}"
        ),
        _context("_Action: verify extraction still produces valid data._"),
    ]
    return send_slack_message(
        blocks, f"Layout change: {source_name} ({agency})")


# --------------------------------------------------------------------------
# Function 4: send_error_alert
# --------------------------------------------------------------------------
def send_error_alert(source_name, agency, error_msg):
    err = (error_msg or "")[:600]
    blocks = [
        _section(":x: *Scraper Error*"),
        _section(
            f"*Source:* {source_name}\n"
            f"*Agency:* {agency}\n"
            f"*Error:* `{err}`\n"
            f"*Time:* {_fmt_ts()}"
        ),
    ]
    return send_slack_message(blocks, f"Scraper error: {source_name}")


# --------------------------------------------------------------------------
# Function 5: send_new_source_alert
# --------------------------------------------------------------------------
def send_new_source_alert(source_name, agency, records, method="HTML extraction"):
    blocks = [
        _section(":white_check_mark: *New Source Scraped*"),
        _section(
            f"*Source:* {source_name}\n"
            f"*Agency:* {agency}\n"
            f"*Records:* `{records:,}`\n"
            f"*Method:* {method}\n"
            f"*Time:* {_fmt_ts()}"
        ),
    ]
    return send_slack_message(
        blocks, f"New source live: {source_name} ({records:,} records)")


# --------------------------------------------------------------------------
# Function 6: send_test_message
# --------------------------------------------------------------------------
def send_test_message():
    host = socket.gethostname()
    blocks = [
        _section(":large_green_circle: *AML Pipeline — Notification System Active*"),
        _section(
            f"*Pipeline:* `risk-pipeline`\n"
            f"*Host:* `{host}`\n"
            f"*Time:* {_fmt_ts()}"
        ),
    ]
    return send_slack_message(
        blocks, "AML Pipeline notification system active.")


# --------------------------------------------------------------------------
# Email: HTML daily report
# --------------------------------------------------------------------------
def _email_config():
    """Return (host, port, sender, password, recipient) or None if any
    env var is missing."""
    cfg = {k: os.environ.get(k) for k in EMAIL_ENV}
    missing = [k for k, v in cfg.items() if not v]
    if missing:
        _log(f"WARN: email skipped; missing env vars: {missing}")
        return None
    try:
        port = int(cfg["EMAIL_SMTP_PORT"])
    except (TypeError, ValueError):
        _log(f"WARN: EMAIL_SMTP_PORT is not an int: {cfg['EMAIL_SMTP_PORT']!r}")
        return None
    return (cfg["EMAIL_SMTP_HOST"], port,
            cfg["EMAIL_SENDER"], cfg["EMAIL_PASSWORD"],
            cfg["EMAIL_RECIPIENT"])


def _html_escape(s):
    if s is None:
        return ""
    return (str(s)
            .replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _build_email_html(stats):
    date_str        = stats.get("date", _ist_now().strftime("%-d %b %Y"))
    total_sources   = stats.get("total_sources", 244)
    completed       = stats.get("completed", 0)
    records_total   = stats.get("records_total", 0)
    records_new     = stats.get("records_new", 0)
    agencies        = stats.get("agencies", 0)
    successful_runs = stats.get("successful_runs", 0)
    failed_runs     = stats.get("failed_runs", 0)
    skipped_runs    = stats.get("skipped_runs", 0)
    layout_changes  = stats.get("layout_changes") or []
    failures        = stats.get("failures") or []

    delta = f"+{records_new:,}" if records_new >= 0 else f"{records_new:,}"
    timestamp = _ist_now().strftime("%-d %b %Y, %I:%M %p IST")

    def kv_row(label, value):
        return (f'<tr>'
                f'<td style="padding:6px 14px 6px 0;color:#475569;'
                f'font-weight:500;width:220px;">{_html_escape(label)}</td>'
                f'<td style="padding:6px 0;color:#0f172a;'
                f'font-family:ui-monospace,Menlo,Consolas,monospace;">'
                f'{_html_escape(value)}</td>'
                f'</tr>')

    def section_h(title, color="#0f172a"):
        return (f'<h3 style="margin:24px 0 8px 0;font-size:15px;'
                f'color:{color};font-weight:600;">{_html_escape(title)}</h3>')

    metrics_rows = "".join([
        kv_row("Sources Completed", f"{completed} / {total_sources}"),
        kv_row("Records in DB", f"{records_total:,}"),
        kv_row("New Records Today", delta),
        kv_row("Agencies Covered", str(agencies)),
    ])
    status_rows = "".join([
        kv_row("Successful", str(successful_runs)),
        kv_row("Failed", str(failed_runs)),
        kv_row("Skipped", str(skipped_runs)),
    ])

    layout_html = ""
    if layout_changes:
        items = "".join(
            f'<li style="margin-bottom:4px;"><strong>'
            f'{_html_escape(lc.get("source","?"))}</strong> &mdash; '
            f'{_html_escape(lc.get("detail","hash changed"))}</li>'
            for lc in layout_changes
        )
        layout_html = (
            section_h(f"⚠️  Layout Changes Detected ({len(layout_changes)})",
                      color="#b45309") +
            f'<ul style="margin:0 0 0 18px;padding:0;color:#0f172a;">{items}</ul>'
        )

    failures_html = ""
    if failures:
        items = "".join(
            f'<li style="margin-bottom:4px;"><strong>'
            f'{_html_escape(f.get("source","?"))}</strong> &mdash; '
            f'<code style="background:#fee2e2;padding:1px 4px;'
            f'border-radius:3px;">{_html_escape((f.get("reason") or "")[:160])}'
            f'</code></li>'
            for f in failures
        )
        failures_html = (
            section_h(f"❌  Failures ({len(failures)})", color="#b91c1c") +
            f'<ul style="margin:0 0 0 18px;padding:0;color:#0f172a;">{items}</ul>'
        )

    if not layout_changes and not failures:
        clean_html = (
            '<p style="color:#16a34a;font-weight:500;margin:18px 0 0 0;">'
            '✅ No layout changes, no failures.</p>'
        )
    else:
        clean_html = ""

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;background:#f1f5f9;
             font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;
             color:#0f172a;">
  <div style="max-width:640px;margin:24px auto;background:#ffffff;
              border:1px solid #e2e8f0;border-radius:10px;overflow:hidden;">

    <!-- Header -->
    <div style="background:#0f172a;color:#f8fafc;padding:18px 24px;">
      <div style="font-size:13px;letter-spacing:1.2px;color:#94a3b8;
                  text-transform:uppercase;font-weight:600;">
        Scoreme &nbsp;|&nbsp; Resurgent India
      </div>
      <div style="font-size:20px;font-weight:600;margin-top:2px;">
        AML Pipeline &mdash; Daily Run Report
      </div>
      <div style="font-size:12px;color:#cbd5e1;margin-top:4px;">
        {_html_escape(date_str)} &nbsp;·&nbsp; {_html_escape(timestamp)}
      </div>
    </div>

    <!-- Body -->
    <div style="padding:20px 24px 28px 24px;">

      {section_h("📈  Pipeline Metrics")}
      <table cellspacing="0" cellpadding="0" style="font-size:14px;">
        {metrics_rows}
      </table>

      {section_h("⚙️  Run Status")}
      <table cellspacing="0" cellpadding="0" style="font-size:14px;">
        {status_rows}
      </table>

      {layout_html}
      {failures_html}
      {clean_html}

    </div>

    <!-- Footer -->
    <div style="background:#f8fafc;color:#64748b;padding:12px 24px;
                font-size:11px;border-top:1px solid #e2e8f0;">
      This is an automated report from the AML Watchlist Scraping Pipeline.
    </div>

  </div>
</body></html>
"""
    return html


def _build_email_text(stats):
    """Plaintext fallback for email clients that don't render HTML."""
    date_str        = stats.get("date", _ist_now().strftime("%-d %b %Y"))
    completed       = stats.get("completed", 0)
    total_sources   = stats.get("total_sources", 244)
    records_total   = stats.get("records_total", 0)
    records_new     = stats.get("records_new", 0)
    delta = f"+{records_new:,}" if records_new >= 0 else f"{records_new:,}"
    layout_changes  = stats.get("layout_changes") or []
    failures        = stats.get("failures") or []

    lines = [
        "AML Pipeline — Daily Run Report",
        f"Date: {date_str}",
        "",
        f"Sources Completed: {completed} / {total_sources}",
        f"Records in DB:     {records_total:,}",
        f"New Records Today: {delta}",
        f"Agencies Covered:  {stats.get('agencies', 0)}",
        "",
        f"Successful: {stats.get('successful_runs', 0)}",
        f"Failed:     {stats.get('failed_runs', 0)}",
        f"Skipped:    {stats.get('skipped_runs', 0)}",
    ]
    if layout_changes:
        lines += ["", f"Layout Changes ({len(layout_changes)}):"]
        for lc in layout_changes:
            lines.append(f"  - {lc.get('source','?')} — {lc.get('detail','hash changed')}")
    if failures:
        lines += ["", f"Failures ({len(failures)}):"]
        for f in failures:
            lines.append(f"  - {f.get('source','?')} — {(f.get('reason') or '')[:160]}")
    if not layout_changes and not failures:
        lines += ["", "No layout changes, no failures."]
    lines += ["", "—",
              "Automated report from the AML Watchlist Scraping Pipeline."]
    return "\n".join(lines)


def send_email_report(stats):
    """Send the HTML daily-summary email. Returns True on success, False
    on any failure (no exceptions propagate)."""
    cfg = _email_config()
    if cfg is None:
        return False
    host, port, sender, password, recipient = cfg

    date_str      = stats.get("date", _ist_now().strftime("%-d %b %Y"))
    completed     = stats.get("completed", 0)
    records_total = stats.get("records_total", 0)
    subject = (f"AML Pipeline Report — {date_str} | "
               f"{completed} sources, {records_total:,} records")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"]    = sender
    msg["To"]      = recipient
    msg.set_content(_build_email_text(stats))
    msg.add_alternative(_build_email_html(stats), subtype="html")

    ctx = ssl.create_default_context()
    try:
        with smtplib.SMTP(host, port, timeout=EMAIL_TIMEOUT) as smtp:
            smtp.ehlo()
            smtp.starttls(context=ctx)
            smtp.ehlo()
            smtp.login(sender, password)
            smtp.send_message(msg)
        _log(f"email sent to {recipient} (subject: {subject})")
        return True
    except smtplib.SMTPAuthenticationError as e:
        _log(f"WARN: Email notification failed: SMTPAuthenticationError "
             f"{e.smtp_code} {e.smtp_error!r}")
    except smtplib.SMTPException as e:
        _log(f"WARN: Email notification failed: {type(e).__name__}: {e}")
    except (TimeoutError, socket.timeout) as e:
        _log(f"WARN: Email notification failed: timeout after {EMAIL_TIMEOUT}s "
             f"({type(e).__name__}: {e})")
    except Exception as e:
        _log(f"WARN: Email notification failed: {type(e).__name__}: {e}")
    return False


# --------------------------------------------------------------------------
# CLI smoke test:  python -m utils.notifier
# --------------------------------------------------------------------------
if __name__ == "__main__":
    print("send_test_message ->", send_test_message())
