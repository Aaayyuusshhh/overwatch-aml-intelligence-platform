"""Other Competent Authorities Debarred Entities (#110),
sourced from BSE's daily download mirror."""
import os
from scrapers.bse_debarred_entities import run_other_authorities_110

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT_FILE = os.path.join(
    PROJECT_ROOT, "data", "sebi_list_of_debarred_entities_110.csv")


def run():
    run_other_authorities_110()


if __name__ == "__main__":
    run()
