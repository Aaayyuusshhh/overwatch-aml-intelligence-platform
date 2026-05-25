#!/usr/bin/env python3
"""MCA ROC Adjudication Orders scraper — penalty orders issued by each ROC office.

Source: https://www.mca.gov.in/content/mca/global/en/data-and-reports/rd-roc-info/roc-adjudication-orders.html

This page does NOT use the standard /bin/dms/searchDocList API. Instead, when a
user selects a specific ROC from the dropdown, the JS posts an AES-encrypted
payload to /bin/mca/ROCAdjudicationOrdersFilter and gets back a JSON list of
orders with full metadata (CIN, CompanyName, OrderNumber, OrderDate, etc).
No PDF parsing required — the JSON has the entity rows directly.

Encryption parameters extracted from clientlibs-encrptdecrypt.min.js:
  passText = "d6163f0659cfe4196dc03c2c29aab06f10cb0a79cdfc74a45da2d72358712e80"
  salt     = MD5("fc74a45dsalt")
  iv       = MD5("c29aab06iv")
  cipher   = AES-128-CBC, PBKDF2-HMAC-SHA1, 100 iterations, PKCS7 pad
"""
from __future__ import annotations
import base64, csv, json, os, time, urllib.parse, warnings
from datetime import datetime, timezone
from hashlib import md5
import requests, urllib3
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7

warnings.filterwarnings("ignore")
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(_ROOT, "data")
os.makedirs(DATA_DIR, exist_ok=True)

SOURCE_ID = "mca_roc_adjudication_orders"
SOURCE_LIST = "ROC Adjudication Orders (Companies Act penalties)"
AGENCY = "Ministry of Corporate Affairs (MCA)"
PAGE_URL = "https://www.mca.gov.in/content/mca/global/en/data-and-reports/rd-roc-info/roc-adjudication-orders.html"
API_URL = "https://www.mca.gov.in/bin/mca/ROCAdjudicationOrdersFilter"

ROCS = [
    "ROC Ahmedabad", "ROC Andaman", "ROC Bangalore", "ROC Chandigarh",
    "ROC Chennai", "ROC Chhattisgarh", "ROC Coimbatore", "ROC Cuttack",
    "ROC Delhi I", "ROC Delhi II", "ROC Ernakulam", "ROC Goa", "ROC Guwahati",
    "ROC Gwalior", "ROC Haryana", "ROC Himachal Pradesh", "ROC Hyderabad",
    "ROC Jaipur", "ROC Jammu", "ROC Kolkata I", "ROC Kolkata II",
    "ROC Mumbai I", "ROC Mumbai II", "ROC Nagpur", "ROC Patna",
    "ROC Pondicherry", "ROC Pune", "ROC Ranchi",
    "ROC Uttar Pradesh I", "ROC Uttar Pradesh II", "ROC Uttarakhand",
    "ROC Vijayawada",
]

FIELDS = ["source_agency", "source_list", "case_unit", "name", "father_name",
          "date_of_birth", "gender", "address", "reward_amount", "details",
          "has_document", "document_url", "detail_page_url",
          "interpol_notice_id", "link_kind", "scraped_at", "enrichment_status"]

_PASS = b"d6163f0659cfe4196dc03c2c29aab06f10cb0a79cdfc74a45da2d72358712e80"
_SALT = md5(b"fc74a45dsalt").digest()
_IV = md5(b"c29aab06iv").digest()
_KEY = PBKDF2HMAC(algorithm=hashes.SHA1(), length=16, salt=_SALT,
                  iterations=100).derive(_PASS)


def _encrypt(msg: str) -> str:
    pad = PKCS7(128).padder()
    padded = pad.update(msg.encode()) + pad.finalize()
    enc = Cipher(algorithms.AES(_KEY), modes.CBC(_IV)).encryptor()
    ct = enc.update(padded) + enc.finalize()
    return urllib.parse.quote(base64.b64encode(ct).decode(), safe='')


H_NAV = {
    "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-IN,en-US;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Sec-Ch-Ua": '"Chromium";v="125", "Not_A Brand";v="24"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Linux"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Upgrade-Insecure-Requests": "1",
}
H_AJAX = {
    "User-Agent": H_NAV["User-Agent"],
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "en-IN,en-US;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Sec-Ch-Ua": H_NAV["Sec-Ch-Ua"],
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Linux"',
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": PAGE_URL,
}


def get_session():
    s = requests.Session()
    s.get("https://www.mca.gov.in/", headers=H_NAV, timeout=30, verify=False)
    time.sleep(1)
    s.get(PAGE_URL, headers=H_NAV, timeout=30, verify=False)
    time.sleep(0.5)
    return s


def fetch_roc(session, roc_name, limit=2000):
    enc = _encrypt(f"ROCOffice={roc_name}&offset=0&limit={limit}")
    url = f"{API_URL}?data={enc}"
    r = session.get(url, headers=H_AJAX, timeout=60, verify=False)
    if r.status_code != 200:
        return []
    try:
        j = r.json()
    except Exception:
        return []
    return j.get("data", []) or []


def order_to_row(order: dict, now: str) -> dict:
    company = (order.get("CompanyName") or "").strip()
    cin = (order.get("CIN") or "").strip()
    order_no = (order.get("OrderNumber") or "").strip()
    order_date = (order.get("OrderDate") or "").strip()
    roc_loc = (order.get("ROCLocation") or "").strip()
    roc_code = (order.get("ROCCode") or "").strip()
    case_no = (order.get("CaseNumber") or "").strip()
    fname = (order.get("AttachmentFileName") or "").strip()
    dms_id = (order.get("AttachmentDMSId") or "").strip()
    category = (order.get("AttachmentCategory") or "").strip()
    label = (order.get("AttachmentLabel") or "").strip()
    fsize = order.get("AttachmentFileSize", "")
    parts = [
        f"CIN: {cin}" if cin else "",
        f"Order No: {order_no}",
        f"Order Date: {order_date}",
        f"ROC: {roc_loc} ({roc_code})",
        f"Case No: {case_no}" if case_no else "",
        f"Category: {category}" if category else "",
        f"Label: {label}" if label else "",
        f"File: {fname}",
        f"Size: {fsize}B" if fsize else "",
    ]
    details = " | ".join(p for p in parts if p)
    doc_url = (f"https://www.mca.gov.in/bin/dms/getdocument?mds={dms_id}"
               if dms_id else "")
    return {
        "source_agency": AGENCY,
        "source_list": SOURCE_LIST,
        "case_unit": roc_loc,
        "name": company,
        "father_name": "",
        "date_of_birth": "",
        "gender": "",
        "address": "",
        "reward_amount": "",
        "details": details,
        "has_document": "Yes" if dms_id else "No",
        "document_url": doc_url,
        "detail_page_url": PAGE_URL,
        "interpol_notice_id": "",
        "link_kind": "pdf" if dms_id else "",
        "scraped_at": now,
        "enrichment_status": "",
    }


def main():
    out_path = os.path.join(DATA_DIR, f"{SOURCE_ID}.csv")
    session = get_session()
    now = datetime.now(timezone.utc).isoformat()
    summary = []
    grand_total = 0
    seen_orders = set()
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        for roc in ROCS:
            orders = fetch_roc(session, roc)
            n = 0
            for o in orders:
                if not isinstance(o, dict):
                    continue
                key = (o.get("CIN", ""), o.get("OrderNumber", ""),
                       o.get("AttachmentDMSId", ""))
                if key in seen_orders:
                    continue
                seen_orders.add(key)
                if not (o.get("CompanyName") or "").strip():
                    continue
                w.writerow(order_to_row(o, now))
                n += 1
            grand_total += n
            summary.append((roc, n))
            print(f"  {roc:30s}  +{n} orders")
            time.sleep(0.6)
    print(f"\n  DONE: {out_path}  rows={grand_total}")
    for roc, n in summary:
        print(f"    {roc:30s}  {n:>5d}")
    return grand_total


if __name__ == "__main__":
    main()
