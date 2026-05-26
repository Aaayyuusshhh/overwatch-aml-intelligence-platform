"""Download OpenSanctions sub-collections (debarment, crime, peps)."""
import os
import time
import requests

os.makedirs("data/opensanctions/", exist_ok=True)
H = {'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) Chrome/126.0 Safari/537.36'}

DATASETS = [
    ("debarment", "debarment.csv"),
    ("crime", "crime.csv"),
    ("peps", "peps.csv"),
]

for slug, filename in DATASETS:
    url = f"https://data.opensanctions.org/datasets/latest/{slug}/targets.simple.csv"
    path = f"data/opensanctions/{filename}"

    if os.path.exists(path) and os.path.getsize(path) > 10000:
        age_h = (time.time() - os.path.getmtime(path)) / 3600
        if age_h < 6:
            with open(path) as f:
                n = sum(1 for _ in f) - 1
            print(f"  SKIP {slug} ({age_h:.1f}h old, {n:,} rows)")
            continue

    print(f"  Downloading {slug}...", end=" ", flush=True)
    try:
        r = requests.get(url, headers=H, timeout=300, stream=True)
        r.raise_for_status()
        with open(path, 'wb') as f:
            for chunk in r.iter_content(8192):
                f.write(chunk)
        with open(path, encoding='utf-8') as f:
            n = sum(1 for _ in f) - 1
        print(f"OK - {os.path.getsize(path):,} bytes, {n:,} rows")
    except Exception as e:
        print(f"FAILED: {e}")
    time.sleep(1)
