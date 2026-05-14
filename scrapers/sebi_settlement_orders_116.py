"""SEBI Settlement Orders (#116). Wraps sebi_enforcement_orders."""
import os
from scrapers.sebi_enforcement_orders import run_smid

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT_FILE = os.path.join(
    PROJECT_ROOT, "data", "sebi_settlement_orders_116.csv")


def run():
    run_smid(smid=3,
             smtext="Settlement Order",
             list_name="Settlement Orders",
             csv_filename="sebi_settlement_orders_116.csv")


if __name__ == "__main__":
    run()
