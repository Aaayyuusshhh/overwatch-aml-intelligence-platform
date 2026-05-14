"""SEBI Orders of Chairperson/Members (#115). Wraps sebi_enforcement_orders."""
import os
from scrapers.sebi_enforcement_orders import run_smid

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT_FILE = os.path.join(
    PROJECT_ROOT, "data", "sebi_orders_of_chairperson_member_115.csv")


def run():
    run_smid(smid=2,
             smtext="Orders of Chairperson/Members",
             list_name="Orders of Chairperson/Members",
             csv_filename="sebi_orders_of_chairperson_member_115.csv")


if __name__ == "__main__":
    run()
