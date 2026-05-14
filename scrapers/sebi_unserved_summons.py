"""SEBI Unserved Summons / Notices (ssid=13, smid=0)."""
import os
from scrapers.sebi_enforcement_orders import run_ssid

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT_FILE  = os.path.join(PROJECT_ROOT, "data", "sebi_members_suspended_113.csv")


def run():
    run_ssid(ssid=13,
             sstext="Unserved Summons/Notices",
             list_name="Unserved Summons/Notices",
             csv_filename="sebi_members_suspended_113.csv")


if __name__ == "__main__":
    run()
