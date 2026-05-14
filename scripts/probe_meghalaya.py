"""Probe Meghalaya Police wanted-person page — static fetch first, look
for block structure."""
import re
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

URL = "https://megpolice.gov.in/wanted-person"
H = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
     "Accept-Language": "en-US,en;q=0.9"}

r = requests.get(URL, timeout=30, verify=False, headers=H)
print(f"status={r.status_code}  len={len(r.text)}")
text = r.text

# Look for common Drupal/CMS structures
for pat, label in [
    (r'class=["\'][^"\']*views-row[^"\']*["\']',    "div.views-row"),
    (r'class=["\'][^"\']*view-content[^"\']*["\']', "div.view-content"),
    (r'<article',                                    "article"),
    (r'class=["\'][^"\']*field-content[^"\']*["\']',"div.field-content"),
    (r'<table',                                      "<table>"),
    (r'class=["\'][^"\']*wanted[^"\']*["\']',       "*.wanted*"),
    (r'class=["\'][^"\']*profile[^"\']*["\']',      "*.profile*"),
    (r'class=["\'][^"\']*card[^"\']*["\']',         "*.card*"),
]:
    n = len(re.findall(pat, text, re.I))
    print(f"  {label:<22} -> {n} matches")

# Find any repeating block by simple heuristic: look for repeated h2/h3 + img
print("\nh2/h3 headings + nearby anchors:")
for m in re.finditer(r"<(h[23])[^>]*>([\s\S]{0,200}?)</\1>", text, re.I):
    h = re.sub(r"<[^>]+>", " ", m.group(2))
    h = re.sub(r"\s+", " ", h).strip()
    if h and len(h) > 3 and len(h) < 120:
        print(f"  {m.group(1)}: {h[:90]}")

# img alt or src patterns
print("\nimages with face/photo-like alts:")
for m in re.finditer(r'<img[^>]*alt=["\']([^"\']+)["\'][^>]*>', text, re.I):
    alt = m.group(1).strip()
    if any(k in alt.lower() for k in ("wanted", "photo", "person", "criminal")):
        print(f"  alt={alt!r}")

# Anchors that look like wanted-person detail links
print("\nAnchors with 'wanted'/'wanted-person'/'wanted-criminal' in href or text:")
seen = set()
for m in re.finditer(r'''<a[^>]+href=["']([^"']+)["'][^>]*>([\s\S]{0,200}?)</a>''', text, re.I):
    href, raw = m.group(1), m.group(2)
    txt = re.sub(r"<[^>]+>", " ", raw); txt = re.sub(r"\s+", " ", txt).strip()
    blob = (href + " " + txt).lower()
    if "wanted" in blob and href not in seen and len(href) < 200:
        seen.add(href)
        print(f"  {href[:100]}")
        print(f"    [{txt[:100]}]")
        if len(seen) >= 15: break

# Pagination
print("\npagination markers:")
for m in re.finditer(r'href=["\']([^"\']*[?&]page=\d+[^"\']*)["\']', text, re.I):
    print(f"  {m.group(1)[:120]}")
