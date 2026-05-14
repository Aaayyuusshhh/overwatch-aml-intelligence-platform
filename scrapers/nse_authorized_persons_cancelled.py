"""NSE Authorized Persons Cancelled (PDF)."""
import os
from scrapers.nse_compliance_actions import run_ap_cancelled

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT_FILE  = os.path.join(PROJECT_ROOT, "data", "nse_authorized_person_ap_cancellation_199.csv")


def run():
    run_ap_cancelled()


if __name__ == "__main__":
    run()
