"""NSE Non-Compliant Promoter Freezing / Z Movement."""
import os
from scrapers.nse_compliance_actions import run_promoter_freezing

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT_FILE = os.path.join(
    PROJECT_ROOT, "data", "nse_promoter_freezing.csv")


def run():
    run_promoter_freezing()


if __name__ == "__main__":
    run()
