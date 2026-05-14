"""SEBI Recovery Proceedings (ssid=50, smid=0). Wraps sebi_enforcement_orders.

Non-canonical source — written under data/extras/ until a PPT slot is
assigned. Counts toward the SEBI agency total once loaded.
"""
import os
from scrapers.sebi_enforcement_orders import run_ssid

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT_FILE  = os.path.join(PROJECT_ROOT, "data", "sebi_investor_alerts_108.csv")


def run():
    run_ssid(ssid=50,
             sstext="Recovery Proceedings",
             list_name="Recovery Proceedings",
             csv_filename="sebi_investor_alerts_108.csv")


if __name__ == "__main__":
    run()
