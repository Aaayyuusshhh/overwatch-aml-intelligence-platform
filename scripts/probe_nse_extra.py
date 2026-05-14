"""Probe the 6 new NSE endpoints to verify they download + see schema."""
import io
import re
import sys
import urllib3

import pandas as pd
import requests

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
H = {"User-Agent": UA, "Referer": "https://www.nseindia.com/",
     "Accept": "*/*", "Accept-Language": "en-US,en;q=0.9"}

URLS = [
    ("non_compliant_equity",
     "https://nsearchives.nseindia.com/corporates/content/SOP_E_Noncompliance.xls"),
    ("promoter_freezing",
     "https://nsearchives.nseindia.com//web/mediaattachment/2026-04/Noncompliant_companies__Promoter_freezing_and_Movement_to_Z_22-04-2026_20260422163135.xlsx"),
    ("icdr_fines",
     "https://nsearchives.nseindia.com//web/mediaattachment/2026-04/ICDR_Fines_17.04.2026_20260421161852.xls"),
    ("defaulting_clients_240",
     "https://nsearchives.nseindia.com/web/sites/default/files/inline-files/Defaulting_Client_Database%202_1_1%20%281%29%20%281%29.xlsx"),
]

s = requests.Session()
s.headers.update(H)

for label, url in URLS:
    print(f"\n=== {label} ===\n  {url}")
    try:
        r = s.get(url, timeout=45, verify=False)
    except Exception as e:
        print(f"  fetch err: {type(e).__name__}: {e}")
        continue
    print(f"  status={r.status_code}  bytes={len(r.content)}  ct={r.headers.get('content-type','?')}")
    if r.status_code != 200 or len(r.content) < 200:
        continue
    parsed = False
    for engine in ("openpyxl", "xlrd", None):
        try:
            kw = {"engine": engine} if engine else {}
            df = pd.read_excel(io.BytesIO(r.content), sheet_name=0,
                               header=None, nrows=10, **kw)
            print(f"  engine={engine!r}  shape={df.shape}")
            for i in range(min(8, len(df))):
                row = df.iloc[i].dropna().tolist()
                if row:
                    print(f"    row {i}: {[str(v)[:60] for v in row[:8]]}")
            parsed = True
            break
        except Exception as e:
            pass
    if not parsed:
        print(f"  COULD NOT PARSE — try html?")
        try:
            tbls = pd.read_html(io.StringIO(r.text))
            print(f"  read_html: {len(tbls)} tables")
            if tbls:
                print(f"    first table shape: {tbls[0].shape}")
                print(tbls[0].head(5).to_string()[:600])
        except Exception as e:
            print(f"  html parse err: {e}")
