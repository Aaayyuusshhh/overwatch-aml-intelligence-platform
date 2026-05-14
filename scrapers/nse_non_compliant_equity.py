"""NSE Non-Compliant Companies (Equity). Wraps nse_compliance_actions."""
import os
from scrapers.nse_compliance_actions import run_non_compliant_equity

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT_FILE  = os.path.join(PROJECT_ROOT, "data", "nse_caution_list_241.csv")


def run():
    run_non_compliant_equity()


if __name__ == "__main__":
    run()
