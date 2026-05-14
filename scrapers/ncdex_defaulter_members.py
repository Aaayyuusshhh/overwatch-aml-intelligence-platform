"""NCDEX Defaulter Members scraper (#186). Wraps ncdex_disciplinary."""
from scrapers.ncdex_disciplinary import (
    run_defaulters as _run, DEFAULTER_OUT as OUTPUT_FILE
)

def run():
    _run()

if __name__ == "__main__":
    run()
