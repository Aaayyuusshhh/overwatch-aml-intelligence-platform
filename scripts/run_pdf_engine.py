"""Run the generic PDF engine for one source given as CLI args.

Usage: python scripts/run_pdf_engine.py <source_id> <agency> <list_name> <url>
"""
import sys
from engines import pdf_scraper

if len(sys.argv) != 5:
    print("usage: run_pdf_engine.py <source_id> <agency> <list_name> <url>")
    sys.exit(2)

sid, agency, list_name, url = sys.argv[1:5]
source = {
    "id": sid,
    "agency": agency,
    "list_name": list_name,
    "url": url,
    "type": "pdf",
}
result = pdf_scraper.run(source)
print(result)
