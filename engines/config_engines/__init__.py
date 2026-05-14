"""Public exports for the config-engine package."""

from . import file_download_engine as FileDownloadEngine
from . import html_table_engine as HtmlTableEngine
from . import api_json_engine as ApiJsonEngine
from . import validator
from . import snapshot_manager
from . import csv_writer

ENGINE_BY_NAME = {
    "file_download": FileDownloadEngine,
    "html_table":    HtmlTableEngine,
    "api_json":      ApiJsonEngine,
}

__all__ = [
    "FileDownloadEngine", "HtmlTableEngine", "ApiJsonEngine",
    "ENGINE_BY_NAME", "validator", "snapshot_manager", "csv_writer",
]
