"""SEBI Auction Notice under Recovery (ssid=79, smid=0)."""
import os
from scrapers.sebi_enforcement_orders import run_ssid

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT_FILE  = os.path.join(PROJECT_ROOT, "data", "sebi_orders_of_aa_under_114.csv")


def run():
    run_ssid(ssid=79,
             sstext="Auction Notice under Recovery",
             list_name="Auction Notice under Recovery",
             csv_filename="sebi_orders_of_aa_under_114.csv")


if __name__ == "__main__":
    run()
