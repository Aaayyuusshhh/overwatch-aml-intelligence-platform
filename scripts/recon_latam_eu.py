#!/usr/bin/env python3
"""Batch-probe LATAM + Europe enforcement URLs concurrently.
Prints one row per URL: status, bytes, ms, tables, links, cloudflare, title/error."""
from __future__ import annotations
import time, warnings, urllib3
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
from bs4 import BeautifulSoup

warnings.filterwarnings("ignore")
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

URLS = [
    # Latin America
    ("brazil_cvm_home",      "https://www.gov.br/cvm/pt-br"),
    ("brazil_cvm_enforce",   "https://www.gov.br/cvm/pt-br/assuntos/regulados/processos-sancionadores"),
    ("mexico_cnbv_home",     "https://www.gob.mx/cnbv"),
    ("mexico_cnbv_enforce",  "https://www.gob.mx/cnbv/acciones-y-programas/sanciones"),
    ("argentina_cnv_home",   "https://www.argentina.gob.ar/cnv"),
    # Europe (not already covered)
    ("bafin_home",           "https://www.bafin.de/EN/"),
    ("bafin_enforce",        "https://www.bafin.de/EN/Aufsicht/BankenFinanzdienstleister/Massnahmen/massnahmen_node_en.html"),
    ("amf_france_home",      "https://www.amf-france.org/en"),
    ("amf_france_enforce",   "https://www.amf-france.org/en/news-publications/news-releases/enforcement-committee"),
    ("consob_home",          "https://www.consob.it/web/consob-and-its-activities/home"),
    ("consob_enforce",       "https://www.consob.it/web/consob-and-its-activities/warnings"),
    ("dnb_home",             "https://www.dnb.nl/en/"),
    ("dnb_enforce",          "https://www.dnb.nl/en/public-register/"),
]
H = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/125.0 Safari/537.36",
     "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
     "Accept-Language": "en;q=0.9,pt;q=0.8,es;q=0.7,fr;q=0.6,de;q=0.6,it;q=0.5,nl;q=0.5"}


def probe(label, url):
    out = {"label": label, "url": url, "status": "-", "bytes": 0,
           "ms": 0, "tables": 0, "links": 0, "cloudflare": False,
           "title": "", "error": ""}
    t0 = time.time()
    try:
        r = requests.get(url, headers=H, timeout=20, verify=False, allow_redirects=True)
        out["ms"] = int((time.time()-t0)*1000)
        out["status"] = r.status_code
        out["bytes"] = len(r.content)
        text = r.text
        if "cloudflare" in text.lower() or "challenge-platform" in text.lower():
            out["cloudflare"] = True
        soup = BeautifulSoup(text, "html.parser")
        if soup.title and soup.title.string:
            out["title"] = soup.title.string.strip()[:60]
        out["tables"] = len(soup.find_all("table"))
        out["links"] = len(soup.find_all("a"))
    except Exception as e:
        out["ms"] = int((time.time()-t0)*1000)
        out["error"] = f"{type(e).__name__}: {str(e)[:80]}"
    return out


def main():
    results = []
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = {ex.submit(probe, lbl, url): lbl for lbl, url in URLS}
        for fut in as_completed(futs, timeout=60):
            results.append(fut.result())
    results.sort(key=lambda r: r["label"])
    print(f"{'label':25s} {'status':6s} {'bytes':>8s} {'ms':>6s} {'tbl':>4s} "
          f"{'links':>5s} {'CF':>3s} title / error")
    for r in results:
        cf = "Y" if r["cloudflare"] else "-"
        title_or_err = r["error"] or r["title"]
        print(f"{r['label']:25s} {str(r['status']):6s} {r['bytes']:>8d} "
              f"{r['ms']:>6d} {r['tables']:>4d} {r['links']:>5d} {cf:>3s} {title_or_err}")


if __name__ == "__main__":
    main()
