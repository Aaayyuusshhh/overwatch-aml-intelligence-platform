#!/usr/bin/env python3
"""MCP Server for RBI ODI (Overseas Direct Investment) data.

Connects to the ODI REST API and exposes company lookup, country
analysis, and aggregate statistics as MCP tools that Claude can call.

Environment variables:
    ODI_API_BASE  Base URL of the ODI REST API (default: http://65.1.148.112:8001)
    ODI_API_KEY   API key sent as X-API-Key header

Usage (stdio transport, intended for Claude Desktop):
    python mcp_servers/odi_mcp_server.py
"""
import asyncio
import json
import os
from collections import defaultdict

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

API_BASE = os.environ.get("ODI_API_BASE", "http://65.1.148.112:8001").rstrip("/")
API_KEY = os.environ.get(
    "ODI_API_KEY", "odi-live-708020d6-e83a-4d1b-8834-0e3b4211037e"
)
HTTP_TIMEOUT = float(os.environ.get("ODI_HTTP_TIMEOUT", "30"))

server = Server("odi-mcp-server")


def _api_get(path: str, params: dict | None = None) -> dict:
    headers = {"X-API-Key": API_KEY}
    url = f"{API_BASE}{path}"
    with httpx.Client(timeout=HTTP_TIMEOUT) as client:
        r = client.get(url, headers=headers, params=params or {})
        r.raise_for_status()
        return r.json()


def _num(v) -> float:
    try:
        return float(v) if v is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def _format_company_report(company_query: str, data: dict) -> str:
    results = data.get("results", [])
    total = data.get("total", len(results))
    if not results:
        return f"No overseas investment records found for '{company_query}'."

    yearly: dict[str, dict] = defaultdict(lambda: {"count": 0, "total_usd_mn": 0.0})
    by_country: dict[str, dict] = defaultdict(lambda: {"count": 0, "total_usd_mn": 0.0})
    by_party: dict[str, int] = defaultdict(int)
    jv_count = 0
    wos_count = 0
    total_usd = 0.0
    total_equity = 0.0
    total_loan = 0.0
    total_guarantee = 0.0

    for r in results:
        pf = r.get("period_from") or ""
        year = pf.split("/")[-1] if "/" in pf else "Unknown"
        amt = _num(r.get("total_usd_mn"))
        country = r.get("country") or "Unknown"

        yearly[year]["count"] += 1
        yearly[year]["total_usd_mn"] += amt

        by_country[country]["count"] += 1
        by_country[country]["total_usd_mn"] += amt

        by_party[r.get("indian_party") or ""] += 1

        if (r.get("jv_or_wos") or "").upper() == "JV":
            jv_count += 1
        else:
            wos_count += 1

        total_usd += amt
        total_equity += _num(r.get("equity_usd_mn"))
        total_loan += _num(r.get("loan_usd_mn"))
        total_guarantee += _num(r.get("guarantee_usd_mn"))

    yearly_sorted = sorted(yearly.items(), key=lambda x: x[0], reverse=True)
    countries_sorted = sorted(
        by_country.items(), key=lambda x: x[1]["total_usd_mn"], reverse=True
    )
    parties_sorted = sorted(by_party.items(), key=lambda x: x[1], reverse=True)

    primary_party = parties_sorted[0][0] if parties_sorted else company_query
    title = primary_party if len(parties_sorted) == 1 else f"matches for '{company_query}'"

    note = ""
    if total > len(results):
        note = f"\n*Note: showing {len(results):,} of {total:,} matching records (per_page cap).*\n"

    distinct_parties_line = ""
    if len(parties_sorted) > 1:
        distinct_parties_line = (
            f"\n**Distinct Indian parties matched:** {len(parties_sorted)} "
            f"(top: {', '.join(p for p, _ in parties_sorted[:5])}"
            f"{' ...' if len(parties_sorted) > 5 else ''})\n"
        )

    lines = [
        f"## Overseas Investment Report: {title}",
        "",
        f"**Total records:** {len(results):,}{note}",
        f"**Total financial commitment:** ${total_usd:,.2f} million USD",
        f"  - Equity: ${total_equity:,.2f}M  |  Loan: ${total_loan:,.2f}M  |  Guarantee: ${total_guarantee:,.2f}M",
        f"**Structure:** {jv_count} Joint Ventures (JV)  |  {wos_count} Wholly Owned Subsidiaries (WOS)",
        f"**Countries:** {len(by_country)}",
        distinct_parties_line.rstrip(),
        "",
        "### Top Destination Countries:",
        "| Country | Investments | Total USD (mn) |",
        "|---|---|---|",
    ]
    for country, info in countries_sorted[:15]:
        lines.append(f"| {country} | {info['count']} | ${info['total_usd_mn']:,.2f} |")

    lines += [
        "",
        "### Year-by-Year Summary:",
        "| Year | Investments | Total USD (mn) |",
        "|---|---|---|",
    ]
    for year, info in yearly_sorted:
        lines.append(f"| {year} | {info['count']} | ${info['total_usd_mn']:,.2f} |")

    lines += ["", "### Recent Investments (most recent 10):"]
    recent = sorted(
        results,
        key=lambda r: (r.get("period_from") or "").split("/")[::-1],
        reverse=True,
    )[:10]
    for r in recent:
        lines.append(
            f"- **{r.get('period_from','')} → {r.get('period_to','')}**: "
            f"{r.get('indian_party','')} → {r.get('jv_wos_name','')} "
            f"({r.get('jv_or_wos','')}) in {r.get('country','')} — "
            f"${_num(r.get('total_usd_mn')):,.4f}M "
            f"[Eq ${_num(r.get('equity_usd_mn')):,.4f}M / Ln ${_num(r.get('loan_usd_mn')):,.4f}M / "
            f"Gt ${_num(r.get('guarantee_usd_mn')):,.4f}M] — {r.get('activity','')}"
        )

    return "\n".join(lines)


def _format_country_report(country: str, company_filter: str | None, data: dict) -> str:
    results = data.get("results", [])
    total = data.get("total", len(results))
    if not results:
        suffix = f" (company filter: {company_filter})" if company_filter else ""
        return f"No investments found for country '{country}'{suffix}."

    by_company: dict[str, dict] = defaultdict(lambda: {"count": 0, "total_usd_mn": 0.0})
    by_activity: dict[str, dict] = defaultdict(lambda: {"count": 0, "total_usd_mn": 0.0})
    yearly: dict[str, dict] = defaultdict(lambda: {"count": 0, "total_usd_mn": 0.0})
    jv_count = 0
    wos_count = 0

    for r in results:
        co = r.get("indian_party") or "(unknown)"
        amt = _num(r.get("total_usd_mn"))
        by_company[co]["count"] += 1
        by_company[co]["total_usd_mn"] += amt

        act = r.get("activity") or "(unknown)"
        by_activity[act]["count"] += 1
        by_activity[act]["total_usd_mn"] += amt

        pf = r.get("period_from") or ""
        year = pf.split("/")[-1] if "/" in pf else "Unknown"
        yearly[year]["count"] += 1
        yearly[year]["total_usd_mn"] += amt

        if (r.get("jv_or_wos") or "").upper() == "JV":
            jv_count += 1
        else:
            wos_count += 1

    total_usd = sum(info["total_usd_mn"] for info in by_company.values())
    top_companies = sorted(
        by_company.items(), key=lambda x: x[1]["total_usd_mn"], reverse=True
    )[:20]
    top_activities = sorted(
        by_activity.items(), key=lambda x: x[1]["total_usd_mn"], reverse=True
    )[:10]
    yearly_sorted = sorted(yearly.items(), key=lambda x: x[0], reverse=True)

    note = ""
    if total > len(results):
        note = f" (showing {len(results):,} of {total:,} matching records)"

    header = f"## Indian Investments in {country}"
    if company_filter:
        header += f" (filtered by company: '{company_filter}')"

    lines = [
        header,
        "",
        f"**Total records:** {total:,}{note}",
        f"**Total commitment (visible records):** ${total_usd:,.2f} million USD",
        f"**JV:** {jv_count}  |  **WOS:** {wos_count}",
        f"**Distinct Indian investors:** {len(by_company)}",
        "",
        "### Top Indian Investors:",
        "| # | Company | Investments | Total USD (mn) |",
        "|---|---|---|---|",
    ]
    for i, (co, info) in enumerate(top_companies, 1):
        lines.append(f"| {i} | {co} | {info['count']} | ${info['total_usd_mn']:,.2f} |")

    lines += [
        "",
        "### Top Activity Sectors:",
        "| Activity | Investments | Total USD (mn) |",
        "|---|---|---|",
    ]
    for act, info in top_activities:
        lines.append(f"| {act} | {info['count']} | ${info['total_usd_mn']:,.2f} |")

    lines += [
        "",
        "### Year-by-Year:",
        "| Year | Investments | Total USD (mn) |",
        "|---|---|---|",
    ]
    for year, info in yearly_sorted:
        lines.append(f"| {year} | {info['count']} | ${info['total_usd_mn']:,.2f} |")

    return "\n".join(lines)


def _format_stats_report(filters: dict, data: dict) -> str:
    lines = ["## RBI ODI Statistics", ""]
    if filters:
        lines.append(f"**Filters:** {json.dumps(filters)}")
        lines.append("")
    lines.append(f"**Total records:** {data.get('total_records', 0):,}")
    lines.append(
        f"**Total commitment:** ${_num(data.get('total_usd_mn')):,.2f} million USD"
    )
    lines.append(f"**Unique companies:** {data.get('unique_companies', 0):,}")
    lines.append(f"**Countries:** {data.get('unique_countries', 0)}")
    pr = data.get("period_range") or {}
    lines.append(f"**Period:** {pr.get('from', '?')} → {pr.get('to', '?')}")
    lines.append("")

    top_countries = data.get("top_countries") or []
    if top_countries:
        lines += [
            "### Top Countries:",
            "| Country | Records | Total USD (mn) |",
            "|---|---|---|",
        ]
        for c in top_countries:
            lines.append(
                f"| {c.get('country','')} | {c.get('records',0):,} | ${_num(c.get('total_usd_mn')):,.2f} |"
            )
        lines.append("")

    top_companies = data.get("top_companies") or []
    if top_companies:
        lines += [
            "### Top Companies:",
            "| Company | Records | Total USD (mn) |",
            "|---|---|---|",
        ]
        for c in top_companies:
            lines.append(
                f"| {c.get('company','')} | {c.get('records',0):,} | ${_num(c.get('total_usd_mn')):,.2f} |"
            )

    return "\n".join(lines)


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="odi_company_lookup",
            description=(
                "Look up overseas direct investments by an Indian company from the "
                "Reserve Bank of India ODI dataset (Jul 2007 - Apr 2026, 104,688 records, "
                "28,512 companies, 173 countries). Returns full investment history: "
                "JV/WOS entities, destination countries, activity sectors, "
                "equity/loan/guarantee amounts in USD millions, country breakdown, "
                "year-by-year trend, and the most recent investments. Partial name "
                "match is supported."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "company_name": {
                        "type": "string",
                        "description": (
                            "Indian company name (partial match supported, e.g. "
                            "'Reliance Industries', 'Tata Steel', 'ONGC')"
                        ),
                    }
                },
                "required": ["company_name"],
            },
        ),
        Tool(
            name="odi_country_analysis",
            description=(
                "Analyze Indian overseas investments into a specific destination "
                "country. Shows top Indian investors, top activity sectors, "
                "year-by-year flows, JV vs WOS split. Optionally filter by a "
                "specific Indian company."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "country": {
                        "type": "string",
                        "description": (
                            "Destination country name (e.g. 'SINGAPORE', "
                            "'UNITED STATES OF AMERICA', 'MAURITIUS', 'UNITED ARAB EMIRATES')"
                        ),
                    },
                    "company": {
                        "type": "string",
                        "description": "Optional: filter to a single Indian company name",
                    },
                },
                "required": ["country"],
            },
        ),
        Tool(
            name="odi_stats",
            description=(
                "Get aggregate statistics on Indian overseas investments: total "
                "records, total USD committed, unique companies, top countries, "
                "top companies. Optionally filter by year, company, or country."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "year": {
                        "type": "integer",
                        "description": "Filter by year (e.g. 2024). Range: 2007-2026.",
                    },
                    "company": {
                        "type": "string",
                        "description": "Filter by Indian company name (partial match)",
                    },
                    "country": {
                        "type": "string",
                        "description": "Filter by destination country",
                    },
                },
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        if name == "odi_company_lookup":
            company = (arguments or {}).get("company_name", "").strip()
            if not company:
                return [TextContent(type="text", text="Error: company_name is required.")]
            data = _api_get(
                "/api/odi/search", {"company": company, "per_page": 1000}
            )
            return [TextContent(type="text", text=_format_company_report(company, data))]

        if name == "odi_country_analysis":
            country = (arguments or {}).get("country", "").strip()
            if not country:
                return [TextContent(type="text", text="Error: country is required.")]
            company_filter = (arguments or {}).get("company", "").strip() or None
            params = {"country": country, "per_page": 1000}
            if company_filter:
                params["company"] = company_filter
            data = _api_get("/api/odi/search", params)
            return [
                TextContent(
                    type="text",
                    text=_format_country_report(country, company_filter, data),
                )
            ]

        if name == "odi_stats":
            args = arguments or {}
            params = {}
            if args.get("year"):
                params["year"] = args["year"]
            if args.get("company"):
                params["company"] = args["company"]
            if args.get("country"):
                params["country"] = args["country"]
            data = _api_get("/api/odi/stats", params)
            return [TextContent(type="text", text=_format_stats_report(params, data))]

        return [TextContent(type="text", text=f"Unknown tool: {name}")]

    except httpx.HTTPStatusError as e:
        return [
            TextContent(
                type="text",
                text=f"ODI API error {e.response.status_code}: {e.response.text[:500]}",
            )
        ]
    except httpx.HTTPError as e:
        return [TextContent(type="text", text=f"Network error calling ODI API: {e}")]
    except Exception as e:
        return [TextContent(type="text", text=f"Error: {e}")]


async def main() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream, write_stream, server.create_initialization_options()
        )


if __name__ == "__main__":
    asyncio.run(main())
