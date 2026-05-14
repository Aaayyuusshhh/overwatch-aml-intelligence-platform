"""One-off probe: dump all anchors near 'Wilful' / 'Large' in IOB page."""
import re
from scrapling import Fetcher

r = Fetcher.get('https://www.iob.bank.in/en/customers-care', timeout=45,
                retries=1, retry_delay=0, verify=False)
body = r.body if hasattr(r, 'body') else r.content
if isinstance(body, bytes):
    body = body.decode('utf-8', 'ignore')
print('len', len(body))

# All anchors whose visible text mentions 'defaulter' (case-insensitive)
seen = set()
for m in re.finditer(
        r'''<a[^>]+href=["']([^"']+)["'][^>]*>([\s\S]{0,400}?)</a>''', body, re.I):
    href, raw_txt = m.group(1), m.group(2)
    txt = re.sub(r'<[^>]+>', ' ', raw_txt)
    txt = re.sub(r'\s+', ' ', txt).strip()
    blob = (href + ' ' + txt).lower()
    if 'defaulter' in blob and href not in seen:
        seen.add(href)
        print(f'  {href}')
        print(f'      [{txt[:140]}]')

# Also: WD_NSF / WD_SF / LD_NSF / LD_SF tokens
print('\nDoc-token search:')
for tok in ('WD_NSF', 'WD_SF', 'LD_NSF', 'LD_SF'):
    for m in re.finditer(re.escape(tok), body):
        i = m.start()
        snippet = body[max(0, i-40):i+200]
        snippet = re.sub(r'<[^>]+>', ' ', snippet)
        snippet = re.sub(r'\s+', ' ', snippet).strip()
        print(f'  {tok}: {snippet[:200]}')
        break  # one per token
