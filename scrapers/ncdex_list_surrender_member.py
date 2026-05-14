"""NCDEX List Surrender Member scraper (#188). Wraps ncdex_disciplinary."""
from scrapers.ncdex_disciplinary import (
    run_surrender as _run, SURRENDER_OUT as OUTPUT_FILE
)

def run():
    _run()

if __name__ == "__main__":
    run()
