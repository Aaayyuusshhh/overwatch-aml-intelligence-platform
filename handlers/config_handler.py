"""
handlers/config_handler.py — handler that main.py dispatches to
when a source has type="config".

Looks up the per-source config file at configs/sources/<id>.json,
validates it against the schema, runs the appropriate engine,
post-validates the extracted records, and writes the canonical
17-column CSV. Returns the same dict shape as the other handlers.

To wire this into main.py, add exactly two lines (NOT applied
automatically):

    from handlers import config_handler

    HANDLER_BY_TYPE = {
        "html":       html_handler.handle,
        "pdf":        pdf_handler.handle,
        "js":         js_handler.handle,
        "restricted": restricted_handler.handle,
        "config":     config_handler.handle,
    }
"""

import json
import os
import time

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIGS_DIR  = os.path.join(PROJECT_ROOT, "configs", "sources")


def _result(status, **extra):
    base = {"status": status, "record_count": 0, "csv_path": None,
            "runtime_seconds": 0.0, "error": None}
    base.update(extra)
    return base


def _load_config(source_id):
    path = os.path.join(CONFIGS_DIR, f"{source_id}.json")
    if not os.path.exists(path):
        return None, f"config file missing: {path}"
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f), None
    except Exception as e:
        return None, f"config file invalid JSON ({type(e).__name__}: {e})"


def handle(source: dict) -> dict:
    t0 = time.time()
    source_id = source.get("id") or source.get("source_id")
    if not source_id:
        return _result("failure", error="source has no id",
                       runtime_seconds=round(time.time() - t0, 2))

    cfg, err = _load_config(source_id)
    if cfg is None:
        return _result("failure", error=err,
                       runtime_seconds=round(time.time() - t0, 2))

    # Schema validation (imports kept local so the handler module loads
    # cleanly even if the engines package has unrelated import errors)
    from configs.config_schema import validate_config
    ok, schema_errors = validate_config(cfg)
    if not ok:
        return _result("failure",
                       error="schema: " + "; ".join(schema_errors),
                       runtime_seconds=round(time.time() - t0, 2))

    # Run the engine
    from engines.config_engines import ENGINE_BY_NAME, validator, csv_writer
    engine_name = cfg["engine"]
    engine = ENGINE_BY_NAME.get(engine_name)
    if engine is None:
        return _result("failure",
                       error=f"unknown engine: {engine_name}",
                       runtime_seconds=round(time.time() - t0, 2))
    try:
        records = engine.run(cfg)
    except Exception as e:
        return _result("failure",
                       error=f"{engine_name}: {type(e).__name__}: {e}",
                       runtime_seconds=round(time.time() - t0, 2))

    # Post-extraction validation gate
    ok, issues = validator.validate_output(records, cfg)
    if not ok:
        return _result("failure",
                       record_count=len(records),
                       error="validation: " + "; ".join(issues),
                       runtime_seconds=round(time.time() - t0, 2))

    # Write CSV
    csv_path = csv_writer.write_records(records, source_id)
    return _result("success",
                   record_count=len(records),
                   csv_path=csv_path,
                   runtime_seconds=round(time.time() - t0, 2))
