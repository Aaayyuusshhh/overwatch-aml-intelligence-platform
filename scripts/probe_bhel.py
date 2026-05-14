"""Probe: find PDF links on BHEL /list-debarred-firms."""
import re
from scrapling import Fetcher

r = Fetcher.get('https://www.bhel.com/list-debarred-firms', timeout=45,
                retries=1, retry_delay=0, verify=False)
body = r.body if hasattr(r, 'body') else r.content
if isinstance(body, bytes):
    body = body.decode('utf-8', 'ignore')
print('status', getattr(r, 'status', None), 'len', len(body))

pdfs = re.findall(r'''href=["']([^"']+\.pdf[^"']*)["']''', body, re.I)
print('total pdfs:', len(pdfs))
for p in pdfs:
    print(' ', p)

# Anchor + visible-text combos
print('\nAnchor+text:')
for m in re.finditer(r'''<a[^>]+href=["']([^"']+\.pdf[^"']*)["'][^>]*>([\s\S]{0,200}?)</a>''',
                     body, re.I):
    href, raw_txt = m.group(1), m.group(2)
    txt = re.sub(r'<[^>]+>', ' ', raw_txt)
    txt = re.sub(r'\s+', ' ', txt).strip()
    print(f'  {href}')
    print(f'      [{txt[:140]}]')
