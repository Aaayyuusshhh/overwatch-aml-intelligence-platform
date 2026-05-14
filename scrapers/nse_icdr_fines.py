"""NSE ICDR Fines."""
import os
from scrapers.nse_compliance_actions import run_icdr_fines

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT_FILE = os.path.join(
    PROJECT_ROOT, "data", "nse_icdr_fines.csv")


def run():
    run_icdr_fines()


if __name__ == "__main__":
    run()
