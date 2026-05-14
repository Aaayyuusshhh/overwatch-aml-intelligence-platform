"""NCDEX Members Inspection Penalty List scraper (#189). Wraps ncdex_disciplinary."""
from scrapers.ncdex_disciplinary import (
    run_penalties as _run, PENALTY_OUT as OUTPUT_FILE
)

def run():
    _run()

if __name__ == "__main__":
    run()
