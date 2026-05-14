"""NCDEX Cessation of Membership scraper (#183). Wraps ncdex_disciplinary."""
from scrapers.ncdex_disciplinary import (
    run_cessation as _run, CESSATION_OUT as OUTPUT_FILE
)

def run():
    _run()

if __name__ == "__main__":
    run()
