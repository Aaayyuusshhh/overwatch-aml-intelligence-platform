"""SEBI Orders That Could Not be Served (ssid=12, smid=0)."""
import os
from scrapers.sebi_enforcement_orders import run_ssid

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT_FILE  = os.path.join(PROJECT_ROOT, "data", "sebi_suspected_shell_companies_117.csv")


def run():
    run_ssid(ssid=12,
             sstext="Orders That Could Not be Served",
             list_name="Orders That Could Not be Served",
             csv_filename="sebi_suspected_shell_companies_117.csv")


if __name__ == "__main__":
    run()
