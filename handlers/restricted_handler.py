"""Restricted source handler. Logs and skips with reason. Never fetches."""


def handle(source):
    reason = source.get("notes") or "marked restricted in sources.json"
    return {"status": "skipped", "record_count": 0, "runtime_seconds": 0.0,
            "error": reason, "csv_path": None}
