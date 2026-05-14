# Remaining-opportunities triage

Generated 2026-05-11 after Part-A reclassification of 8 sources
(#63–66 → dead_url, #111/112/118 → skipped, #132 → network_error).

## Status distribution

| Status          | Count |
|-----------------|------:|
| active          |   95  |
| url_not_found   |   77  |
| skipped         |   24  |
| js              |   17  |
| restricted      |   16  |
| dead_url        |   16  |
| network_error   |    2  |
| dead            |    2  |
| failed          |    1  |
| **TOTAL**       | **250** |

77 of 250 are `url_not_found` (already exhausted by the URL-discovery
sweep) and 16 are `dead_url`. The realistic surface area for new
records is the union of {`js`, `failed`, `restricted`, `network_error`}
plus a tail of `skipped` entries whose sibling source might be parsed
differently.

## JS sources — static-probe results

| #   | Agency / List                              | Size  | Tables | PDFs | Notes |
|----:|--------------------------------------------|------:|-------:|-----:|-------|
|   2 | AICTE Unapproved Institutes                |   —   |    —   |   —  | **No URL configured** — needs discovery sweep |
|   6 | CBIC Central Excise – Fraud                | 3.4K  |  0     |  0   | URL redirects to cbic.gov.in homepage (dead route) |
|   7 | CBIC Central Excise – Penalty/Interest     | 3.4K  |  0     |  0   | same — dead route |
|   8 | CBIC Customs – Fraud/Collusion             | 3.4K  |  0     |  0   | same — dead route |
|   9 | CBIC Customs – Penalty/Interest            | 3.4K  |  0     |  0   | same — dead route |
|  10 | CBIC Customs – Seizure                     | 3.4K  |  0     |  0   | same — dead route |
|  16 | CVC Penalties for Prosecution              | 1.2K  |  0     |  0   | stub response (probably 404 served as 200) |
|  17 | CCI Antitrust Orders                       |  58K  |  1     |  0   | Table header present, data rows AJAX-loaded |
|  24 | ED Red Corner Notices                      |  42K  |  0     |  0   | 17 photo `<img>` tags, no `<h*>` names; Umbraco search API `/umbraco/surface/SearchSurface/QuickSearch?q=` exposed |
|  35 | Income Tax Tax Defaulters                  | 568K  |  0     |  0   | Big body, 2 "defaulter" divs — uses non-table card markup, probably extractable from static HTML once the right selector is found |
| 141 | DMRC Blacklisted Agencies                  | 6.3K  |  0     |  0   | tiny shell — likely 404-as-200 |
| 142 | DMRC Debarred (Metro Parking)              | 6.3K  |  0     |  0   | same |
| 143 | DMRC Debarred/Ineligible/Banned/Suspended  | 6.3K  |  0     |  0   | same |
| 209 | Delhi Police EOW Proclaimed Offenders      |  34K  |  0     |  0   | No tables, no AJAX hooks. Probably iframe-loaded |
| 215 | Kerala Police Wanted Persons               | 271K  |  2     |  2   | Big body with two `<table>` opens and PDF links; data in `<div>` cards |
| 234 | AMFI Application Ref Suspended/Terminated  |   —   |    —   |   —  | **No URL configured** — needs discovery sweep |
| 237 | BSE Arbitration Awards                     |  13K  |  0     |  0   | Angular SPA. Same shell pattern as #160 — the data is baked into `main-*.js` |

---

## TIER 1 — HIGH CONFIDENCE (likely scrappable today)

These have visible data structure or a proven extraction pattern from
an adjacent source.

- **#237 BSE Arbitration Awards** — same Angular SPA as #160. We
  already have a JS-bundle parser
  (`scrapers/bse_defaulter_expelled_members.py`); generalising it for
  the `app-arbitration-awards` component should yield several hundred
  award rows. Risk: low.
- **#215 Kerala Police Wanted Persons** — 271 KB static HTML with two
  tables and embedded PDF links. Likely just needs BeautifulSoup with
  the right table selector. Risk: low.
- **#35 Income Tax Tax Defaulters** — 568 KB static HTML with
  defaulter-card markup. Inspect the static DOM, find the card
  container class, parse with BeautifulSoup. Risk: low/medium.
- **#17 CCI Antitrust Orders** — empty `<tbody>` plus a `Search Any
  Case` widget suggests an AJAX endpoint. Look for the JSON XHR in the
  page's `<script>` and call it directly. Risk: medium.

## TIER 2 — MEDIUM CONFIDENCE (needs DevTools recon)

- **#24 ED Red Corner Notices** — Umbraco search endpoint
  `/umbraco/surface/SearchSurface/QuickSearch?q=` is exposed in the
  HTML. Each notice carries a photo `<img>`; the notice metadata is
  almost certainly loaded by a parallel XHR. Open Network tab and grab
  the JSON endpoint.
- **#209 Delhi Police EOW Proclaimed Offenders** — 34 KB shell with
  no tables and no obvious AJAX hook. Need to open in Chrome and
  inspect the actual rendered table; the iframe URL or AJAX call will
  surface there.
- **#2 AICTE Unapproved Institutes** — no URL configured at all. Need
  a discovery probe of `aicte-india.org` enforcement pages.
- **#234 AMFI Application Reference Number Suspended/Terminated** —
  same story, no URL configured. AMFI publishes intermediary action
  notices; the discovery sweep should target `amfiindia.com`.
- **#4 BIS Enforcement | Judgements** (`failed`, not `js`) — the
  `/enforcement/?lang=hi` URL returns 235 KB of real content. Needs
  recon to find the actual list link from that page.

## TIER 3 — LOW CONFIDENCE (likely won't yield)

These should be reclassified to `dead_url` or `superseded` rather
than spending effort.

- **#6 / #7 / #8 / #9 / #10 CBIC list pages** — all five redirect to
  the CBIC homepage. CBIC stopped publishing these as standalone list
  pages. Either find the data in their tariff/orders archive or
  reclassify all five.
- **#16 CVC Penalties for Prosecution** — 1.2 KB stub.
- **#141 / #142 / #143 DMRC** — all three 6.3 KB stubs (same shell).
  Either DMRC restructured these or they're paginated behind a portal
  we can't reach.
- **#237** flagged as Tier 1 above; here only because its overlap
  with CBIC-style "JS dead route" was a near miss.

## Restricted bucket — credentials worth pursuing?

16 sources are flagged `restricted`. Most are walled and unobtainable:
- MCA V3 portal lists (**#40, 41, 48, 49, 52, 53, 55**) — 7 sources,
  blanket HTTP 403 against MCA's V3 portal. MCA has periodically
  exposed *some* of these as bulk downloads or via the Master Data
  download flow; worth a one-off check if anyone on the team has an
  MCA login.
- **#18 / 19 CIBIL Suit-Filed** — commercial subscription.
  Unobtainable without a paying CIR/CMR account.
- **#27 ESIC Defaulters**, **#33 GeM Watchlist/Defaulter**,
  **#86 / 87 NCRB Missing/PO**, **#93 / 94 NHB lists**, **#180 MCX
  Surrendered** — most are search-only or login-gated. Not realistically
  recoverable.

## Skipped bucket — recheck candidates

A few `skipped` entries marked themselves blocked but might still be
parseable today:
- **#11 CBI Fugitive Economic Offender** — CBI publishes these on
  their list-of-most-wanted endpoint; we already pull CBI Most Wanted
  (#206). Probably duplicative.
- **#31 FIU Orders** — separate from the FIU Compliance Orders /
  Judgements we already scrape (#fiu_compliance_orders). Worth one
  more URL discovery pass.
- **#102 NIA Reward List** — NIA publishes most-wanted with rewards;
  current pull is "NIA Most Wanted" (#100). Check whether reward list
  is a separate page.
- **#194, 197 NSEL** — both `skipped`; we already have NSEL crystallised
  amount + arbitration data, but decrees-obtained list (#197) is
  distinct and could yield additional defaulter records.

## Realistic path to 90+ active sources

Today's count is 79. Tier 1 (4 sources) + recheckable skipped (3-4
sources) = realistically **+5–8 active sources** (85–87 total) without
manual recon. Adding Tier 2 follow-throughs would bring the total
into the low-90s. Hitting 100+ requires either MCA credentials,
manual XLSX downloads (like we did for FIU NBFC PDFs), or new URL
discovery for the two `no URL` JS entries (#2 AICTE, #234 AMFI).
