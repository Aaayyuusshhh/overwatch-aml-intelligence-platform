"""SEBI Orders of AO (#119). Wraps sebi_enforcement_orders."""
import os
from scrapers.sebi_enforcement_orders import run_smid

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT_FILE = os.path.join(
    PROJECT_ROOT, "data", "sebi_orders_of_ao_119.csv")


def run():
    run_smid(smid=6,
             smtext="Orders of AO",
             list_name="Orders of AO",
             csv_filename="sebi_orders_of_ao_119.csv")


if __name__ == "__main__":
    run()
