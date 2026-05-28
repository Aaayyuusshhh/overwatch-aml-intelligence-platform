"""
Overwatch AML — daily pipeline report.

Queries the DB for fresh metrics, compares against yesterday's source_health
snapshot, and sends two outputs:
  - Professional HTML email via AWS SES (region from .env)
  - Rich Slack message (Block Kit) via webhook from .env

Designed to run at the end of run_all.sh on EC2, after scraping/loading is done.
Tolerant of partial failures: if SES fails, Slack still goes out (and vice versa).

Exit code: 0 on success, 1 if BOTH channels failed.
"""
from __future__ import annotations
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_PATH = os.path.join(PROJECT_ROOT, ".env")
LOG_DIR = os.path.join(PROJECT_ROOT, "logs")
DIFF_PATH = os.path.join(LOG_DIR, "post_scrape_diff.json")
os.makedirs(LOG_DIR, exist_ok=True)


def load_pipeline_diff() -> dict:
    """Load logs/post_scrape_diff.json written by scripts/compare_counts.py.

    Returns an empty {} if the file is missing (older run, or a run where
    compare_counts didn't execute). The daily report degrades gracefully —
    it just won't show per-source deltas in that case.
    """
    if not os.path.exists(DIFF_PATH):
        return {}
    try:
        with open(DIFF_PATH) as f:
            return json.load(f)
    except Exception:
        return {}

# Indian Standard Time for "today" reporting (the team is in IST).
IST = timezone(timedelta(hours=5, minutes=30))


# --------------------------------------------------------------------- env

def load_env() -> dict:
    """Minimal .env parser — no shell expansion, no comments mid-line."""
    env = dict(os.environ)
    if not os.path.exists(ENV_PATH):
        return env
    with open(ENV_PATH) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            env.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    return env


ENV = load_env()


def fmt(n: int | None) -> str:
    return f"{n:,}" if isinstance(n, int) else "-"


def fmt_short(n: int | None) -> str:
    """4,843,710 -> '4.84M' ; 113,888 -> '114K' ; 676 -> '676'."""
    if not isinstance(n, int):
        return "-"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 10_000:
        return f"{n / 1_000:.0f}K"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


# --------------------------------------------------------------------- db

import psycopg2  # noqa: E402


def db_connect():
    return psycopg2.connect(
        host=ENV.get("PG_HOST", "localhost"),
        user=ENV.get("PG_USER", "aayush"),
        password=ENV.get("PG_PASSWORD", "aayush123"),
        dbname=ENV.get("PG_DB", "risk_pipeline"),
        connect_timeout=15,
    )


def collect_stats() -> dict:
    out: dict = {}
    with db_connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM watchlist_records;")
        out["total_records"] = cur.fetchone()[0]

        cur.execute("SELECT COUNT(DISTINCT source_id) FROM watchlist_records WHERE source_id <> '';")
        out["sources_active"] = cur.fetchone()[0]

        cur.execute("SELECT COUNT(DISTINCT source_agency) FROM watchlist_records WHERE source_id <> '';")
        out["agencies"] = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM entity_groups;")
        out["kg_groups"] = cur.fetchone()[0]

        cur.execute("SELECT risk_level, COUNT(*) FROM entity_groups GROUP BY risk_level;")
        out["kg_risk"] = {lvl or "(null)": n for lvl, n in cur.fetchall()}
        out["kg_high_risk"] = out["kg_risk"].get("HIGH", 0)

        # records loaded in the last 24h
        cur.execute("SELECT COUNT(*) FROM watchlist_records WHERE loaded_at > NOW() - INTERVAL '24 hours';")
        out["new_today"] = cur.fetchone()[0]

        # top sources by new records in last 24h
        cur.execute("""
            SELECT source_id, COUNT(*) AS n
            FROM watchlist_records
            WHERE loaded_at > NOW() - INTERVAL '24 hours' AND source_id <> ''
            GROUP BY source_id ORDER BY n DESC LIMIT 10;
        """)
        out["new_today_breakdown"] = cur.fetchall()

        # top 10 sources by total record count
        cur.execute("""
            SELECT source_id, COALESCE(source_agency,'(unknown)') AS agency, COUNT(*) AS n
            FROM watchlist_records
            WHERE source_id <> '' GROUP BY source_id, source_agency
            ORDER BY n DESC LIMIT 10;
        """)
        out["top_sources"] = cur.fetchall()

        # most recent scrape ts
        cur.execute("SELECT MAX(scraped_at) FROM watchlist_records WHERE scraped_at <> '';")
        out["last_scrape"] = cur.fetchone()[0] or "n/a"

        # source_health: pull today's snapshot (if any) and surface BROKEN/ANOMALY
        try:
            cur.execute("""
                SELECT source_id, status, notes, agency, list_name
                FROM source_health
                WHERE snapshot_date = (
                    SELECT MAX(snapshot_date) FROM source_health
                )
                AND status IN ('BROKEN','ANOMALY')
                ORDER BY source_id LIMIT 25;
            """)
            out["alerts"] = cur.fetchall()
        except psycopg2.Error:
            out["alerts"] = []

        # source_health latest snapshot date for the report header
        try:
            cur.execute("SELECT MAX(snapshot_date) FROM source_health;")
            out["health_snapshot_date"] = cur.fetchone()[0]
        except psycopg2.Error:
            out["health_snapshot_date"] = None

    # total registered sources (from sources.json — includes blocked/dead)
    sources_json = os.path.join(PROJECT_ROOT, "sources.json")
    try:
        with open(sources_json) as f:
            out["total_registered"] = len(json.load(f).get("sources", []))
    except Exception:
        out["total_registered"] = out["sources_active"]

    # Per-source change diff produced by scripts/compare_counts.py.
    out["pipeline_diff"] = load_pipeline_diff()
    return out


# --------------------------------------------------------------------- html

def _suggest_action(status: str, notes: str | None) -> str:
    """Heuristic 'what to do about it' line for an alert."""
    notes_lc = (notes or "").lower()
    if status == "BROKEN":
        if "0" in notes_lc or "drop" in notes_lc or "missing" in notes_lc:
            return "Source URL may have changed or site is blocking automated access."
        return "Verify the source URL and the scraper output."
    if status == "ANOMALY":
        return "Row count moved more than the configured threshold — confirm the source layout still parses."
    if status == "STALE":
        return "No fresh data in 7+ days — confirm the scraper is still scheduled and the upstream list updates."
    return "Review the source log and re-run the scraper if needed."


def render_html(s: dict, now_ist: datetime) -> str:
    """Premium-SaaS-style daily report.

    Two modes (all-clear navy/blue, alerts dark-red/red). Shared layout for
    everything below the status banner. No em or en dashes anywhere in copy.
    """
    FONT = ("-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,"
            "'Helvetica Neue',Arial,sans-serif")
    BODY    = "#334155"   # slate-700
    SECONDARY = "#64748B" # slate-500

    alerts   = s.get("alerts") or []
    n_alerts = len(alerts)
    is_alert = n_alerts > 0

    # ── theme tokens ────────────────────────────────────────────────────
    if is_alert:
        grad_a, grad_b = "#991B1B", "#DC2626"
        status_icon  = "⚠️"
        status_color = "#DC2626"
        status_title = (
            f"{n_alerts} Alert{'s' if n_alerts != 1 else ''} "
            f"Require{'s' if n_alerts == 1 else ''} Attention"
        )
        status_subtitle = (
            f"Your AML intelligence pipeline has flagged <strong>{n_alerts}</strong> "
            f"issue{'s' if n_alerts != 1 else ''} during today's scheduled scan that "
            f"require your review."
        )
    else:
        grad_a, grad_b = "#1B3A6B", "#2563EB"
        status_icon  = "✅"
        status_color = "#16A34A"
        status_title = "All Systems Operational"
        status_subtitle = (
            "Your AML intelligence pipeline completed its scheduled scan successfully. "
            f"All <strong>{s['sources_active']}</strong> active sources across "
            "<strong>50+ countries</strong> have been verified with zero anomalies detected."
        )

    # Subtle gradient separator (used between sections)
    sep = (
        '<div style="height:1px;background:linear-gradient(to right,'
        'transparent,#E2E8F0,transparent);margin:0 32px"></div>'
    )

    # ── header ──────────────────────────────────────────────────────────
    header = (
        f'<div style="background:linear-gradient(135deg,{grad_a},{grad_b});padding:30px 32px">'
        '<table width="100%" cellpadding="0" cellspacing="0" border="0"><tr>'
        '<td valign="top">'
        '<h1 style="color:#ffffff;margin:0;font-size:20px;font-weight:700;'
        f'letter-spacing:2px;font-family:{FONT}">OVERWATCH AML</h1>'
        '<p style="color:rgba(255,255,255,0.80);margin:6px 0 0;font-size:12px;'
        'letter-spacing:0.4px">Intelligence Platform</p>'
        '<p style="color:rgba(255,255,255,0.5);font-size:10px;margin:3px 0 0">'
        'Powered by Resurgent India</p>'
        '</td>'
        '<td valign="top" align="right">'
        '<p style="color:rgba(255,255,255,0.85);font-size:12px;margin:0;'
        f'letter-spacing:0.3px">{now_ist.strftime("%A, %d %B %Y")}</p>'
        '</td>'
        '</tr></table></div>'
    )

    # ── status banner ───────────────────────────────────────────────────
    banner = (
        '<div style="padding:30px 32px 18px">'
        '<table cellpadding="0" cellspacing="0" border="0"><tr>'
        f'<td style="font-size:24px;vertical-align:middle;padding-right:12px">{status_icon}</td>'
        '<td style="vertical-align:middle">'
        f'<h2 style="color:{status_color};margin:0;font-size:18px;font-weight:600;'
        f'font-family:{FONT}">{status_title}</h2>'
        '</td></tr></table>'
        f'<p style="color:{BODY};font-size:14px;line-height:1.6;margin:14px 0 0;'
        f'font-family:{FONT}">{status_subtitle}</p>'
        '</div>'
    )

    # ── alert cards (only in alert mode) ────────────────────────────────
    alerts_html = ""
    if is_alert:
        cards = []
        for sid, status, notes, agency, list_name in alerts:
            agency_label = agency or sid
            list_label = list_name or ""
            issue_text = (notes or f"Status changed to {status}").strip()
            action_text = _suggest_action(status or "", notes).rstrip(".")
            title_html = (
                f'{agency_label}'
                + (f' <span style="color:{SECONDARY};font-weight:400">. {list_label}</span>'
                   if list_label else "")
            )
            cards.append(
                '<div style="margin:0 32px 12px;border-left:4px solid #DC2626;'
                'border-radius:4px;padding:16px 20px;background:#FEF2F2">'
                f'<h3 style="color:#991B1B;margin:0 0 6px;font-size:14px;font-weight:600;'
                f'font-family:{FONT}">⚠️ {title_html}</h3>'
                f'<p style="color:{BODY};font-size:13px;margin:0 0 4px;line-height:1.5">'
                f'<strong>Issue:</strong> {issue_text}</p>'
                f'<p style="color:{SECONDARY};font-size:12px;margin:0;line-height:1.5">'
                f'<strong>Recommended Action:</strong> {action_text}. '
                'This may affect screening accuracy until resolved.</p>'
                '</div>'
            )
        alerts_html = "".join(cards)
        ok_count = max(0, s["sources_active"] - n_alerts)
        alerts_html += (
            f'<p style="padding:6px 32px 4px;color:{SECONDARY};font-size:13px;margin:0;'
            f'font-family:{FONT};font-style:italic">'
            f'All other <strong>{ok_count}</strong> sources are operating normally.'
            '</p>'
        )

    # ── KPI strip ───────────────────────────────────────────────────────
    def kpi(value: str, label: str, accent: str) -> str:
        return (
            '<td style="text-align:center;padding:0;vertical-align:top">'
            '<div style="background:#FFFFFF;border:1px solid #E2E8F0;border-radius:10px;'
            'overflow:hidden">'
            f'<div style="height:3px;background:{accent}"></div>'
            '<div style="padding:18px 8px">'
            f'<div style="font-size:26px;font-weight:700;color:#1B3A6B;font-family:{FONT};'
            'line-height:1.1">' + value + '</div>'
            f'<div style="font-size:10px;color:#94A3B8;margin-top:6px;text-transform:uppercase;'
            'letter-spacing:0.6px">' + label + '</div>'
            '</div></div></td>'
        )

    kpi_html = (
        '<div style="padding:18px 32px 20px;background:linear-gradient(180deg,#FFFFFF,#F8FAFC)">'
        '<table width="100%" cellpadding="0" cellspacing="0" border="0" '
        'style="border-collapse:separate;border-spacing:8px 0"><tr>'
        + kpi(fmt(s["sources_active"]),     "Sources Monitored",   "#2563EB")
        + kpi(fmt_short(s["total_records"]), "Records in DB",       "#4F46E5")
        + kpi(fmt(s["total_registered"]),    "Registered Sources",  "#7C3AED")
        + kpi(fmt_short(s["kg_high_risk"]),  "High-Risk Entities",  "#DC2626")
        + '</tr></table></div>'
    )

    # ── about ───────────────────────────────────────────────────────────
    # ── today's data changes (from compare_counts.py diff) ─────────────
    diff = s.get("pipeline_diff") or {}
    changes_html = ""
    if diff:
        added = diff.get("added") or []
        removed = diff.get("removed") or []
        zeroed = diff.get("zeroed") or []
        failed = diff.get("failed_scrapers") or []
        fc = diff.get("fatf_changes") or {}
        fatf_any = any(fc.get(k) for k in ("black_added", "black_removed",
                                           "grey_added", "grey_removed"))
        delta_total = diff.get("delta_total", 0)

        if added or removed or zeroed or failed or fatf_any or delta_total:
            rows = []
            if delta_total:
                arrow = "&#x25B2;" if delta_total > 0 else "&#x25BC;"
                color = "#16A34A" if delta_total > 0 else "#DC2626"
                rows.append(
                    f'<tr><td style="padding:6px 0;color:{BODY};font-size:13px">'
                    f'<strong>Net change</strong></td>'
                    f'<td style="padding:6px 0;text-align:right;color:{color};font-size:13px;font-weight:600">'
                    f'{arrow} {delta_total:+,} records</td></tr>'
                )
            for r in added[:10]:
                rows.append(
                    f'<tr><td style="padding:4px 0;color:{BODY};font-size:13px">'
                    f'{r["source_id"]}</td>'
                    f'<td style="padding:4px 0;text-align:right;color:#16A34A;font-size:13px">'
                    f'{r["pre"]:,} &rarr; {r["post"]:,} (+{r["delta"]:,})</td></tr>'
                )
            if len(added) > 10:
                rows.append(
                    f'<tr><td colspan="2" style="padding:4px 0;color:{SECONDARY};'
                    f'font-size:12px;font-style:italic">'
                    f'+{len(added) - 10} more sources gained rows</td></tr>'
                )
            for r in removed[:5]:
                rows.append(
                    f'<tr><td style="padding:4px 0;color:{BODY};font-size:13px">'
                    f'{r["source_id"]}</td>'
                    f'<td style="padding:4px 0;text-align:right;color:#DC2626;font-size:13px">'
                    f'{r["pre"]:,} &rarr; {r["post"]:,} ({r["delta"]:,})</td></tr>'
                )
            for z in zeroed[:5]:
                rows.append(
                    f'<tr><td style="padding:4px 0;color:#991B1B;font-size:13px;font-weight:600">'
                    f'&#x26A0; {z["source_id"]}</td>'
                    f'<td style="padding:4px 0;text-align:right;color:#991B1B;font-size:13px">'
                    f'dropped to ZERO ({z["pre"]:,} &rarr; 0)</td></tr>'
                )
            for fs in failed[:5]:
                rows.append(
                    f'<tr><td colspan="2" style="padding:4px 0;color:#92400E;font-size:13px">'
                    f'&#x26A0; scraper failure: {fs}</td></tr>'
                )
            if fatf_any:
                bits = []
                if fc.get("black_added"):   bits.append("Black list added: " + ", ".join(fc["black_added"]))
                if fc.get("black_removed"): bits.append("Black list removed: " + ", ".join(fc["black_removed"]))
                if fc.get("grey_added"):    bits.append("Grey list added: " + ", ".join(fc["grey_added"]))
                if fc.get("grey_removed"):  bits.append("Grey list removed: " + ", ".join(fc["grey_removed"]))
                rows.append(
                    f'<tr><td colspan="2" style="padding:6px 0;color:#1B3A6B;font-size:13px">'
                    f'<strong>FATF list changes:</strong> {"; ".join(bits)}</td></tr>'
                )

            changes_html = (
                '<div style="padding:22px 32px">'
                f'<h3 style="color:#1B3A6B;margin:0 0 12px;font-size:15px;'
                f'font-weight:600;font-family:{FONT}">Today\'s data changes</h3>'
                '<table width="100%" cellpadding="0" cellspacing="0" border="0">'
                + "".join(rows)
                + '</table></div>'
            )

    about = (
        '<div style="padding:22px 32px;background:#F8FAFC">'
        f'<p style="color:{SECONDARY};font-size:13px;line-height:1.65;margin:0;'
        f'font-family:{FONT}">'
        '<strong style="color:#1B3A6B">Overwatch AML</strong> is an automated intelligence '
        f'platform that monitors <strong>{s["total_registered"]}</strong> global watchlists, '
        'sanctions databases, and regulatory enforcement actions across '
        '<strong>50+ countries</strong>. It continuously scrapes, deduplicates, and '
        'cross-references entities through a proprietary Knowledge Graph, enabling '
        'real-time AML/KYC compliance screening for high-risk individuals and '
        'organizations.'
        '</p></div>'
    )

    # ── footer ──────────────────────────────────────────────────────────
    footer = (
        '<div style="padding:18px 32px;text-align:center;border-top:1px solid #E2E8F0">'
        f'<p style="color:#475569;font-size:11px;margin:0 0 4px;font-weight:600;'
        f'font-family:{FONT}">Next scheduled run: Tomorrow, 6:00 AM IST</p>'
        '<p style="color:#94A3B8;font-size:10px;margin:0;font-family:' + FONT + '">'
        'Overwatch AML Intelligence Platform &nbsp;·&nbsp; Resurgent India Limited '
        '&nbsp;·&nbsp; Confidential'
        '</p></div>'
    )

    # ── assemble ────────────────────────────────────────────────────────
    return (
        f'<div style="font-family:{FONT};max-width:600px;margin:0 auto;background:#ffffff;'
        'border-radius:12px;overflow:hidden;box-shadow:0 4px 24px rgba(0,0,0,0.08)">'
        + header
        + banner
        + (alerts_html if is_alert else "")
        + sep
        + kpi_html
        + (sep + changes_html if changes_html else "")
        + sep
        + about
        + footer
        + '</div>'
    )


# --------------------------------------------------------------------- email

def send_email(subject: str, html_body: str) -> tuple[bool, str]:
    try:
        import boto3
        ses = boto3.client("ses", region_name=ENV.get("SES_REGION", "ap-south-1"))
        resp = ses.send_email(
            Source=ENV.get("SES_FROM", "aayush.katyal@resurgentindia.com"),
            Destination={"ToAddresses": [ENV.get("SES_TO", "aayush.katyal@resurgentindia.com")]},
            Message={
                "Subject": {"Data": subject, "Charset": "UTF-8"},
                "Body": {"Html": {"Data": html_body, "Charset": "UTF-8"}},
            },
        )
        return True, resp.get("MessageId", "")
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:200]}"


# --------------------------------------------------------------------- slack

def _diff_blocks(diff: dict, max_rows: int = 6) -> list[dict]:
    """Build Slack blocks from the per-source diff. Returns [] if nothing
    interesting to say (e.g. first run with no pre-snapshot)."""
    if not diff:
        return []
    added = diff.get("added") or []
    removed = diff.get("removed") or []
    zeroed = diff.get("zeroed") or []
    failed = diff.get("failed_scrapers") or []
    fc = diff.get("fatf_changes") or {}
    fatf_any = any(fc.get(k) for k in ("black_added", "black_removed",
                                       "grey_added", "grey_removed"))
    delta_total = diff.get("delta_total", 0)

    if not (added or removed or zeroed or failed or fatf_any) and delta_total == 0:
        return []

    lines: list[str] = []
    if delta_total:
        lines.append(f"*Net change today:* {delta_total:+,} records")
    if added:
        rows = ", ".join(f"`{r['source_id']}` +{r['delta']:,}" for r in added[:max_rows])
        more = "" if len(added) <= max_rows else f" _(+{len(added) - max_rows} more)_"
        lines.append(f"*New records ({len(added)} source{'s' if len(added) != 1 else ''}):* {rows}{more}")
    if removed:
        rows = ", ".join(f"`{r['source_id']}` {r['delta']:,}" for r in removed[:max_rows])
        more = "" if len(removed) <= max_rows else f" _(+{len(removed) - max_rows} more)_"
        lines.append(f"*Rows removed ({len(removed)}):* {rows}{more}")
    if zeroed:
        rows = ", ".join(f"`{z['source_id']}`" for z in zeroed[:max_rows])
        lines.append(f":rotating_light: *Sources went to ZERO ({len(zeroed)}):* {rows}")
    if failed:
        rows = ", ".join(f"`{f}`" for f in failed[:max_rows])
        lines.append(f":warning: *Scrapers reported failures:* {rows}")
    if fatf_any:
        bits = []
        if fc.get("black_added"):   bits.append(f"black +{', '.join(fc['black_added'])}")
        if fc.get("black_removed"): bits.append(f"black -{', '.join(fc['black_removed'])}")
        if fc.get("grey_added"):    bits.append(f"grey +{', '.join(fc['grey_added'])}")
        if fc.get("grey_removed"):  bits.append(f"grey -{', '.join(fc['grey_removed'])}")
        lines.append(f":globe_with_meridians: *FATF list changed:* {'; '.join(bits)}")

    return [
        {"type": "divider"},
        {"type": "section",
         "text": {"type": "mrkdwn", "text": "*Today's data changes*\n" + "\n".join(lines)}},
    ]


def build_slack_payload(s: dict, now_ist: datetime) -> dict:
    """Slack daily message. All-clear path always includes the diff if any
    real changes happened; alert path keeps the alert list and appends diff
    underneath."""
    alerts = s.get("alerts") or []
    n_alerts = len(alerts)
    diff = s.get("pipeline_diff") or {}
    total_short = fmt_short(s["total_records"])
    high_short  = fmt_short(s["kg_high_risk"])
    date_str    = now_ist.strftime("%A, %d %B %Y")
    diff_extra  = _diff_blocks(diff)

    if n_alerts == 0:
        blocks = [
            {"type": "header",
             "text": {"type": "plain_text", "text": "✅ Overwatch AML — All Systems Operational"}},
            {"type": "section", "text": {"type": "mrkdwn", "text":
                f"Pipeline ran successfully. All *{s['sources_active']}* sources checked — "
                f"no issues detected.\n\n"
                f"*{s['sources_active']}* sources monitored · *{total_short}* records · "
                f"*{s['total_registered']}* registered · *{high_short}* high-risk entities"}},
        ]
        blocks.extend(diff_extra)
        blocks.append({"type": "context", "elements": [
            {"type": "mrkdwn",
             "text": f"_Overwatch AML · Resurgent India · {date_str} · Next run: Tomorrow 6:00 AM IST_"}
        ]})
        return {"blocks": blocks}

    # Alerts mode
    blocks = [
        {"type": "header",
         "text": {"type": "plain_text",
                  "text": f"⚠️ Overwatch AML — {n_alerts} Alert{'s' if n_alerts != 1 else ''} Detected"}},
        {"type": "section",
         "text": {"type": "mrkdwn", "text": "The daily pipeline detected issues:"}},
    ]
    for sid, status, notes, agency, lst in alerts[:10]:
        agency_label = agency or sid
        list_label = f" — {lst}" if lst else ""
        detail = (notes or f"Status: {status}").strip()
        blocks.append({"type": "section", "text": {"type": "mrkdwn",
                         "text": f"*{agency_label}*{list_label}\n> {detail[:200]}"}})
    if len(alerts) > 10:
        blocks.append({"type": "section", "text": {"type": "mrkdwn",
                         "text": f"_…and {len(alerts) - 10} more (see email)_"}})
    ok_count = max(0, s["sources_active"] - n_alerts)
    blocks.append({"type": "divider"})
    blocks.append({"type": "section", "text": {"type": "mrkdwn", "text":
        f"All other *{ok_count}* sources healthy · *{total_short}* records · *{high_short}* high-risk"}})
    blocks.extend(diff_extra)
    blocks.append({"type": "context", "elements": [
        {"type": "mrkdwn", "text": f"_Overwatch AML · Resurgent India · {date_str}_"}
    ]})
    return {"blocks": blocks}


def send_slack(payload: dict) -> tuple[bool, str]:
    webhook = ENV.get("SLACK_WEBHOOK_URL", "")
    if not webhook:
        return False, "no SLACK_WEBHOOK_URL in env"
    try:
        import urllib.request
        req = urllib.request.Request(
            webhook,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return resp.status == 200 and body.strip() == "ok", f"http={resp.status} body={body[:60]}"
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:200]}"


# --------------------------------------------------------------------- main

def main() -> int:
    now_ist = datetime.now(IST)
    t0 = time.time()
    print(f"[{now_ist.isoformat(timespec='seconds')}] collecting stats...")
    s = collect_stats()
    print(f"  total_records={s['total_records']:,}  sources={s['sources_active']}"
          f"  new24h={s['new_today']:,}  kg_groups={s['kg_groups']:,}  alerts={len(s['alerts'])}")

    n_alerts = len(s["alerts"])
    if n_alerts:
        subject = f"⚠️ Overwatch AML — {n_alerts} Alert{'s' if n_alerts != 1 else ''} Require{'s' if n_alerts == 1 else ''} Attention"
    else:
        subject = "✅ Overwatch AML — All Systems Operational"
    html_body = render_html(s, now_ist)
    slack_payload = build_slack_payload(s, now_ist)

    print("sending email...")
    ok_email, email_info = send_email(subject, html_body)
    print(f"  email: ok={ok_email}  detail={email_info}")

    print("sending slack...")
    ok_slack, slack_info = send_slack(slack_payload)
    print(f"  slack: ok={ok_slack}  detail={slack_info}")

    log_path = os.path.join(LOG_DIR, "daily_report.log")
    with open(log_path, "a") as f:
        f.write(
            f"{now_ist.isoformat(timespec='seconds')}\t"
            f"records={s['total_records']}\tsources={s['sources_active']}\t"
            f"new24h={s['new_today']}\talerts={n_alerts}\t"
            f"email_ok={ok_email}\tslack_ok={ok_slack}\t"
            f"took={time.time()-t0:.1f}s\n"
        )
    if ok_email or ok_slack:
        print(f"Report sent successfully in {time.time()-t0:.1f}s")
        return 0
    print("Both channels failed.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
