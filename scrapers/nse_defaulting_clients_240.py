"""NSE Defaulting Clients (#240). Wraps nse_compliance_actions."""
import os
from scrapers.nse_compliance_actions import run_defaulting_clients_240

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT_FILE = os.path.join(
    PROJECT_ROOT, "data", "nse_defaulting_clients_240.csv")


def run():
    run_defaulting_clients_240()


if __name__ == "__main__":
    run()
