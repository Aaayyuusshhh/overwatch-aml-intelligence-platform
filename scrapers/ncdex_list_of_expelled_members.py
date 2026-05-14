"""NCDEX List of Expelled Members scraper (#187). Wraps ncdex_disciplinary."""
from scrapers.ncdex_disciplinary import (
    run_expelled as _run, EXPELLED_OUT as OUTPUT_FILE
)

def run():
    _run()

if __name__ == "__main__":
    run()
