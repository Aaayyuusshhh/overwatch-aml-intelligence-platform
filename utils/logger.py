"""
Structured logging for the orchestrator and handlers.

Per ARCHITECTURE.md §3.5 / PRD §6.10:
  - One log file per run: logs/run_YYYY-MM-DD_HH-MM.log
  - Same lines also written to stdout
  - Structured one-liner format: timestamp | level | source_id | action | detail

Singleton logger configured on first call. Subsequent calls reuse it.
"""

import logging
import os
from datetime import datetime

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGS_DIR = os.path.join(PROJECT_ROOT, "logs")

_LOGGER = None
_LOG_FILE_PATH = None


def get_logger():
    """Return the singleton risk_pipeline logger, configuring on first call."""
    global _LOGGER, _LOG_FILE_PATH
    if _LOGGER is not None:
        return _LOGGER

    os.makedirs(LOGS_DIR, exist_ok=True)
    _LOG_FILE_PATH = os.path.join(
        LOGS_DIR, datetime.now().strftime("run_%Y-%m-%d_%H-%M.log")
    )

    logger = logging.getLogger("risk_pipeline")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    # Reset handlers in case the module is re-imported in the same process.
    logger.handlers.clear()

    fmt = logging.Formatter(
        "%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    fh = logging.FileHandler(_LOG_FILE_PATH, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    _LOGGER = logger
    return logger


def get_log_file_path():
    """Return the path of the current run's log file (None before first call)."""
    return _LOG_FILE_PATH


def log_event(source_id, action, detail="", level="info"):
    """Structured one-liner. source_id may be None for run-level events."""
    logger = get_logger()
    sid = source_id or "-"
    msg = f"{sid:<35}  {action:<22}  {detail}".rstrip()
    getattr(logger, level)(msg)
