#!/usr/bin/env python3
"""Add LATAM+EU sources to sources.json: 1 active (AMF blacklists), 6 blocked."""
import json, os

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_PATH = os.path.join(PROJECT, "sources.json")

NEW_SOURCES = [
    # Active
    {
        "id": "amf_france_blacklists",
        "agency": "Autorité des marchés financiers (AMF)",
        "list_name": "Blacklists of unauthorised companies",
        "url": "https://www.amf-france.org/en/warnings/blacklists",
        "type": "html",
        "scraper": "scrapers/amf_france_blacklists.py",
        "expected_min_records": 10,
        "status": "active",
        "change_detection": False,
        "country": "FR",
        "notes": "AMF blacklist of unauthorised forex/crypto/binary-options entities. HTML table + PDF refs.",
    },
    # Blocked — recon 2026-05-22
    {"id": "mexico_cnbv_sanciones",
     "agency": "Comisión Nacional Bancaria y de Valores (CNBV)",
     "list_name": "Sanciones",
     "url": "https://www.gob.mx/cnbv/acciones-y-programas/sanciones",
     "type": "blocked", "scraper": None, "status": "blocked",
     "change_detection": False, "country": "MX",
     "failure_reason": "cloudflare_challenge",
     "notes": "Cloudflare 'Challenge Validation' page on every request"},
    {"id": "dnb_netherlands_register",
     "agency": "De Nederlandsche Bank (DNB)",
     "list_name": "Public Register",
     "url": "https://www.dnb.nl/en/public-register/",
     "type": "blocked", "scraper": None, "status": "blocked",
     "change_detection": False, "country": "NL",
     "failure_reason": "http_403_access_denied",
     "notes": "Returns 403 Access Denied for non-browser requests"},
    {"id": "bafin_germany_massnahmen",
     "agency": "BaFin (Federal Financial Supervisory Authority)",
     "list_name": "Massnahmen (Enforcement)",
     "url": "https://www.bafin.de/EN/Aufsicht/BankenFinanzdienstleister/Massnahmen/massnahmen_node_en.html",
     "type": "url_not_found", "scraper": None, "status": "url_not_found",
     "change_detection": False, "country": "DE",
     "failure_reason": "url_not_found",
     "notes": "Original URL returns 404; homepage works but enforcement index moved"},
    {"id": "consob_italy_warnings",
     "agency": "Commissione Nazionale per le Società e la Borsa (CONSOB)",
     "list_name": "Warnings",
     "url": "https://www.consob.it/web/consob-and-its-activities/warnings",
     "type": "blocked", "scraper": None, "status": "blocked",
     "change_detection": False, "country": "IT",
     "failure_reason": "rate_limited",
     "notes": "Rate-limits after first request; first hit OK, follow-ups receive 'you reached this page' anti-bot page"},
    {"id": "brazil_cvm_processos",
     "agency": "Comissão de Valores Mobiliários (CVM)",
     "list_name": "Processos Sancionadores",
     "url": "https://www.gov.br/cvm/pt-br/assuntos/regulados/processos-sancionadores",
     "type": "url_not_found", "scraper": None, "status": "url_not_found",
     "change_detection": False, "country": "BR",
     "failure_reason": "url_not_found",
     "notes": "404 behind Cloudflare; CVM homepage works, sanctions page moved"},
    {"id": "argentina_cnv",
     "agency": "Comisión Nacional de Valores (CNV)",
     "list_name": "Sanciones / Resoluciones",
     "url": "https://www.argentina.gob.ar/cnv",
     "type": "blocked", "scraper": None, "status": "blocked",
     "change_detection": False, "country": "AR",
     "failure_reason": "cloudflare_javascript_required",
     "notes": "Homepage loads but is Cloudflare-protected with JS-rendered content"},
]


def main():
    with open(SRC_PATH) as f:
        data = json.load(f)
    existing_ids = {s.get("id") for s in data["sources"]}
    added, skipped = 0, 0
    for new in NEW_SOURCES:
        if new["id"] in existing_ids:
            print(f"  skip (exists): {new['id']}")
            skipped += 1
            continue
        data["sources"].append(new)
        print(f"  add: {new['id']:30s} status={new['status']}")
        added += 1
    with open(SRC_PATH, "w") as f:
        json.dump(data, f, indent=2)
    print(f"\nAdded {added}, skipped {skipped}. Total sources: {len(data['sources'])}")


if __name__ == "__main__":
    main()
