"""NSE Members with Inadequate Networth (HTML table on regs page)."""
import os
from scrapers.nse_compliance_actions import run_members_inadequate_networth

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT_FILE  = os.path.join(PROJECT_ROOT, "data", "nse_list_of_defaulter_members_200.csv")


def run():
    run_members_inadequate_networth()


if __name__ == "__main__":
    run()
