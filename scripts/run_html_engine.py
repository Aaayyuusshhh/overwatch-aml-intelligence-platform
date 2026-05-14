"""Run the generic HTML engine for one source.

Usage: python -m scripts.run_html_engine <source_id> <agency> <list_name> <url>
"""
import sys
from engines import html_scraper

if len(sys.argv) != 5:
    print("usage: run_html_engine.py <source_id> <agency> <list_name> <url>")
    sys.exit(2)

sid, agency, list_name, url = sys.argv[1:5]
source = {
    "id": sid,
    "agency": agency,
    "list_name": list_name,
    "url": url,
    "type": "html",
}
result = html_scraper.run(source)
print(result)
