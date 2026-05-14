"""Single-shot probe of all 9 new endpoints to plan the scrapers."""
import requests, urllib3, io, zipfile
import pandas as pd
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

H_BSE = {"User-Agent": UA, "Referer": "https://www.bseindia.com/", "Accept": "*/*"}
H_NSE = {"User-Agent": UA, "Referer": "https://www.nseindia.com/", "Accept": "*/*",
         "Accept-Language": "en-US,en;q=0.9"}

def probe_zip(url):
    print(f"\n--- ZIP: {url}")
    r = requests.get(url, headers=H_BSE, timeout=30, verify=False)
    print(f"  status={r.status_code} bytes={len(r.content)} ct={r.headers.get('content-type','?')}")
    if r.status_code != 200 or len(r.content) < 200:
        return
    try:
        z = zipfile.ZipFile(io.BytesIO(r.content))
        print(f"  zip members: {z.namelist()}")
        for nm in z.namelist():
            with z.open(nm) as f:
                data = f.read()
            print(f"    -> {nm}: {len(data)} bytes")
            if nm.lower().endswith((".xls",".xlsx")):
                try:
                    df = pd.read_excel(io.BytesIO(data), sheet_name=0, header=None, nrows=8)
                    print(f"       shape={df.shape}")
                    for i in range(min(5, len(df))):
                        row = df.iloc[i].dropna().tolist()
                        if row: print(f"       row {i}: {row[:6]}")
                except Exception as e:
                    print(f"       xlsx read err: {e}")
            elif nm.lower().endswith(".csv"):
                txt = data.decode('utf-8', 'ignore')[:400]
                print(f"       csv head: {txt}")
    except Exception as e:
        print(f"  not a zip / parse err: {e}")
        # try direct excel
        try:
            df = pd.read_excel(io.BytesIO(r.content), sheet_name=0, header=None, nrows=5)
            print(f"  direct xlsx shape={df.shape}")
        except Exception as e2:
            print(f"  not xlsx either: {e2}")

def probe_xls(url, headers=H_BSE):
    print(f"\n--- XLS: {url}")
    r = requests.get(url, headers=headers, timeout=30, verify=False)
    print(f"  status={r.status_code} bytes={len(r.content)} ct={r.headers.get('content-type','?')}")
    if r.status_code != 200 or len(r.content) < 200:
        return
    for engine in ("openpyxl","xlrd",None):
        try:
            kw = {"engine": engine} if engine else {}
            df = pd.read_excel(io.BytesIO(r.content), sheet_name=0, header=None, nrows=10, **kw)
            print(f"  engine={engine!r} shape={df.shape}")
            for i in range(min(8, len(df))):
                row = df.iloc[i].dropna().tolist()
                if row: print(f"    row {i}: {[str(v)[:60] for v in row[:7]]}")
            return
        except Exception as e:
            print(f"  engine={engine!r} err: {type(e).__name__}: {str(e)[:120]}")

# Group 1: BSE
probe_zip("https://www.bseindia.com/download/DebarredEntities/SEBI%20DEBARRED%2007052026.zip")
probe_zip("https://www.bseindia.com/download/DebarredEntities/Other%20Competent%20Authorities%20DEBARRED%2007052026.zip")
probe_xls("https://www.bseindia.com/Downloads1/Action_taken_against_trading_members.xls")

# Group 2: NSE
probe_xls("https://nsearchives.nseindia.com/content/press/prs_ra_sebi.xls", H_NSE)
probe_xls("https://nsearchives.nseindia.com/content/press/prs_ra_others.xls", H_NSE)

# Group 3: SEBI ssid endpoint
print("\n--- SEBI ssid=50 (Recovery Proceedings) ---")
ENDPOINT = "https://www.sebi.gov.in/sebiweb/ajax/home/getnewslistinfo.jsp"
H_SEBI = {"User-Agent": UA, "X-Requested-With": "XMLHttpRequest",
          "Referer": "https://www.sebi.gov.in/enforcement.html",
          "Content-Type": "application/x-www-form-urlencoded"}
payload = {"nextValue":"0","next":"n","search":"","fromDate":"","toDate":"",
           "fromYear":"","toYear":"","deptId":"-1","sid":"2","ssid":"50",
           "smid":"0","ssidhidden":"50","intmid":"-1","sText":"Enforcement",
           "ssText":"Recovery Proceedings","smText":"","doDirect":"-1"}
r = requests.post(ENDPOINT, data=payload, headers=H_SEBI, timeout=30, verify=False)
print(f"  status={r.status_code} len={len(r.text)}")
import re
m = re.search(r"name=['\"]totalpage['\"][^>]*value=(\d+)", r.text)
print(f"  totalpage={m.group(1) if m else '?'}")
import bs4
soup = bs4.BeautifulSoup(r.text, "html.parser")
trs = soup.select("table tr")
print(f"  table rows: {len(trs)}")
for tr in trs[:4]:
    tds = tr.find_all("td")
    if not tds: continue
    print(f"    {[t.get_text(strip=True)[:60] for t in tds]}")
