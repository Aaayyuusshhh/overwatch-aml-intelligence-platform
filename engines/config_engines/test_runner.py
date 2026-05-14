"""
engines/config_engines/test_runner.py — load a single config, run
its engine, validate, print a report.

Does NOT write to data/ (no CSV produced) and does NOT touch
sources.json or the database. Use it to iterate on configs before
flipping a source to type=config.

Run:
  venv/bin/python3 engines/config_engines/test_runner.py \
      --config configs/sources/test_ofac_sdn.json
"""

import argparse
import json
import os
import sys
import time

# Allow running as a script (`python engines/config_engines/test_runner.py`)
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from configs.config_schema import validate_config              # noqa: E402
from engines.config_engines import ENGINE_BY_NAME, validator   # noqa: E402


def _print_sample(records, n=3):
    for i, r in enumerate(records[:n], start=1):
        nm = (r.get("name") or "")[:80]
        det = (r.get("details") or "")[:120]
        print(f"  sample {i}: name={nm!r}")
        print(f"            details={det!r}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True,
                    help="path to a single config JSON")
    args = ap.parse_args()

    print(f"Config: {args.config}")
    with open(args.config, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    ok, schema_errors = validate_config(cfg)
    if not ok:
        print("Engine: (skipped — schema invalid)")
        for e in schema_errors:
            print(f"  • {e}")
        sys.exit(1)

    engine_name = cfg["engine"]
    print(f"Engine: {engine_name}")
    print(f"URL:    {cfg.get('url', '')}")
    engine = ENGINE_BY_NAME[engine_name]
    t0 = time.time()
    try:
        records = engine.run(cfg)
    except Exception as e:
        print(f"Engine raised {type(e).__name__}: {e}")
        sys.exit(2)
    dt = time.time() - t0
    print(f"Records extracted: {len(records)} in {dt:.1f}s")

    ok, issues = validator.validate_output(records, cfg)
    if ok:
        print("Validation: PASSED")
    else:
        print("Validation: FAILED")
        for it in issues:
            print(f"  • {it}")

    if records:
        print("Sample records:")
        _print_sample(records, 3)

    print(f"Raw snapshot dir: snapshots/raw/{cfg['source_id']}/")
    sys.exit(0 if ok else 3)


if __name__ == "__main__":
    main()
