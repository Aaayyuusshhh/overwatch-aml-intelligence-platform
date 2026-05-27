#!/usr/bin/env python3
"""MCP Server for the AML Screening API.

Exposes screen_name and screen_bulk tools that fuzzy-match a name or
company against 6.4M+ watchlist records (OFAC, UN, EU, FATF, Interpol,
OpenSanctions, RBI, SEBI, MCA, FIU, ICIJ leaks, ...) and return a
risk level (HIGH / MEDIUM / LOW / CLEAR) plus the matches that drove it.

Environment variables:
    SCREENING_API_BASE  Base URL of the screening REST API
                        (default: http://65.1.148.112:8002)
    SCREENING_API_KEY   API key sent as X-API-Key header

Usage (stdio transport, intended for Claude Desktop):
    python mcp_servers/screening_mcp_server.py
"""
import asyncio
import os

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

API_BASE = os.environ.get("SCREENING_API_BASE", "http://65.1.148.112:8002").rstrip("/")
API_KEY = os.environ.get("SCREENING_API_KEY", "")
HTTP_TIMEOUT = float(os.environ.get("SCREENING_HTTP_TIMEOUT", "60"))

server = Server("screening-mcp-server")


def _api(method: str, path: str, *, params: dict | None = None,
         json: dict | None = None) -> dict:
    headers = {"X-API-Key": API_KEY} if API_KEY else {}
    url = f"{API_BASE}{path}"
    with httpx.Client(timeout=HTTP_TIMEOUT) as client:
        r = client.request(method, url, headers=headers, params=params, json=json)
        r.raise_for_status()
        return r.json()


RISK_EMOJI = {"HIGH": "🔴", "MEDIUM": "🟠", "LOW": "🟢", "CLEAR": "⚪"}


def _format_screen(r: dict) -> str:
    risk = r.get("risk_level", "UNKNOWN")
    emoji = RISK_EMOJI.get(risk, "")
    lines = [
        f"## Screening Report: {r.get('query', '')}",
        "",
        f"**Risk Level:** {emoji} **{risk}**",
        f"**Watchlist Matches:** {r.get('total_matches', 0)}",
        f"**Screening Time:** {r.get('screening_time_ms', 0)} ms",
    ]

    fatf = r.get("fatf_jurisdiction_flag")
    if fatf:
        lines += [
            "",
            f"### FATF Jurisdiction Flag",
            f"**{fatf.get('list', '?').upper()} LIST**: {fatf.get('name', '')}",
            fatf.get("details", "") or "",
        ]

    matches = r.get("matches") or []
    if matches:
        lines += [
            "",
            "### Top Matches",
            "| Name | Sim | Source | Category |",
            "|---|---|---|---|",
        ]
        for m in matches[:15]:
            name = (m.get("name") or "").replace("|", "\\|")
            agency = (m.get("source_agency") or "").replace("|", "\\|")
            list_name = (m.get("source_list") or "").replace("|", "\\|")
            source = f"{agency} / {list_name}" if list_name else agency
            cat = m.get("risk_category") or ""
            lines.append(f"| {name} | {m.get('similarity', 0):.2f} | {source} | {cat} |")

    odi = r.get("odi_cross_reference") or {}
    if odi.get("found"):
        lines += [
            "",
            "### ODI Cross-Reference (RBI Overseas Investments)",
            f"**Investments:** {odi.get('total_investments', 0)}",
            f"**Total USD:** ${odi.get('total_usd_mn', 0):,.2f} million",
            f"**Countries:** {', '.join(odi.get('countries', []))}",
        ]
        tops = odi.get("top_investments") or []
        if tops:
            lines += ["", "Top investments:"]
            for t in tops:
                amt = float(t.get("total_usd_mn") or 0)
                lines.append(
                    f"- {t.get('indian_party', '')} → {t.get('jv_wos_name', '')} "
                    f"({t.get('country', '')}) ${amt:,.2f}M [{t.get('period_from', '')}]"
                )

    if not matches and not odi.get("found"):
        lines += ["", "*No matches found above the similarity threshold.*"]

    return "\n".join(lines)


def _format_bulk(r: dict) -> str:
    results = r.get("results") or []
    if not results:
        return "No results."
    lines = [
        "## Bulk Screening Results",
        "",
        f"**Total screened:** {r.get('total_screened', 0)}  |  "
        f"**Total time:** {r.get('total_time_ms', 0)} ms",
        "",
        "| Name | Risk | Matches | ODI? | FATF |",
        "|---|---|---|---|---|",
    ]
    for x in results:
        emoji = RISK_EMOJI.get(x.get("risk_level", ""), "")
        odi = x.get("odi_cross_reference") or {}
        odi_txt = f"yes ({odi.get('total_investments', 0)})" if odi.get("found") else "no"
        fatf = x.get("fatf_jurisdiction_flag")
        fatf_txt = fatf.get("list", "").upper() if fatf else ""
        name = (x.get("query") or "").replace("|", "\\|")
        lines.append(
            f"| {name} | {emoji} {x.get('risk_level', '')} | "
            f"{x.get('total_matches', 0)} | {odi_txt} | {fatf_txt} |"
        )

    # Per-name HIGH/MEDIUM details
    flagged = [x for x in results if x.get("risk_level") in ("HIGH", "MEDIUM")]
    for x in flagged:
        lines += ["", f"### {x.get('query', '')} — {x.get('risk_level', '')}"]
        for m in (x.get("matches") or [])[:5]:
            lines.append(
                f"- {m.get('name', '')} (sim {m.get('similarity', 0):.2f}) "
                f"— {m.get('source_agency', '')} / {m.get('source_list', '')} "
                f"[{m.get('risk_category', '')}]"
            )
    return "\n".join(lines)


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="screen_name",
            description=(
                "Screen a single name (person or company) against 6.4M+ watchlist "
                "records including OFAC, UN, EU, UK, FATF, Interpol, OpenSanctions "
                "(PEPs/debarment/crime), RBI/SEBI/MCA/FIU enforcement actions, "
                "ICIJ leaks, and Indian wanted/disqualified lists. Returns risk "
                "level (HIGH/MEDIUM/LOW/CLEAR), all matches with similarity scores, "
                "and ODI cross-reference for overseas investments."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Name or company to screen (e.g. 'Huawei Technologies', 'John Smith')",
                    },
                    "type": {
                        "type": "string",
                        "enum": ["company", "person", "auto"],
                        "description": "Entity type hint (default: auto)",
                    },
                    "threshold": {
                        "type": "number",
                        "description": "Similarity threshold 0.1-1.0 (default 0.6; lower = more matches)",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Max matches to return (default 20, max 200)",
                    },
                },
                "required": ["name"],
            },
        ),
        Tool(
            name="screen_bulk",
            description=(
                "Screen multiple names at once (up to 50). Returns a summary table "
                "with risk levels and details on every HIGH/MEDIUM hit."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "names": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of names/companies to screen",
                        "minItems": 1,
                        "maxItems": 50,
                    },
                    "threshold": {
                        "type": "number",
                        "description": "Similarity threshold 0.1-1.0 (default 0.6)",
                    },
                },
                "required": ["names"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        if name == "screen_name":
            args = arguments or {}
            payload = {"name": args.get("name", "").strip()}
            if not payload["name"]:
                return [TextContent(type="text", text="Error: name is required.")]
            if args.get("type"):
                payload["type"] = args["type"]
            if "threshold" in args:
                payload["threshold"] = float(args["threshold"])
            if "max_results" in args:
                payload["max_results"] = int(args["max_results"])
            data = _api("POST", "/api/screen", json=payload)
            return [TextContent(type="text", text=_format_screen(data))]

        if name == "screen_bulk":
            args = arguments or {}
            names = args.get("names") or []
            if not names:
                return [TextContent(type="text", text="Error: names list is required.")]
            payload = {"names": [{"name": n} for n in names if n and n.strip()]}
            if "threshold" in args:
                payload["threshold"] = float(args["threshold"])
            data = _api("POST", "/api/screen/bulk", json=payload)
            return [TextContent(type="text", text=_format_bulk(data))]

        return [TextContent(type="text", text=f"Unknown tool: {name}")]

    except httpx.HTTPStatusError as e:
        return [TextContent(
            type="text",
            text=f"Screening API error {e.response.status_code}: {e.response.text[:500]}"
        )]
    except httpx.HTTPError as e:
        return [TextContent(type="text", text=f"Network error: {e}")]
    except Exception as e:
        return [TextContent(type="text", text=f"Error: {e}")]


async def main() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream, write_stream, server.create_initialization_options()
        )


if __name__ == "__main__":
    asyncio.run(main())
