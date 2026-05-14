"""
engines/config_engines/snapshot_manager.py — save raw downloaded
content for evidence + rollback. Every config-engine fetch passes
its raw bytes here so future runs can diff against the last known
good payload.
"""

import os
import re
from datetime import datetime

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
SNAP_DIR = os.path.join(PROJECT_ROOT, "snapshots", "raw")


def _safe_ext(extension):
    """Normalise extension: no leading dot, strip query strings, default 'bin'."""
    ext = (extension or "bin").lstrip(".").split("?")[0].lower()
    ext = re.sub(r"[^a-z0-9_-]", "", ext) or "bin"
    return ext


def save_raw_snapshot(source_id: str, content: bytes,
                        extension: str = "bin") -> str:
    """Persist `content` under snapshots/raw/{source_id}/{timestamp}.{ext}.
    Also updates snapshots/raw/{source_id}/latest.{ext} to point at the
    new file (best-effort symlink; on Windows we copy)."""
    ext = _safe_ext(extension)
    sid = re.sub(r"[^A-Za-z0-9_.-]", "_", source_id)
    dirpath = os.path.join(SNAP_DIR, sid)
    os.makedirs(dirpath, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    fname = f"{ts}.{ext}"
    out = os.path.join(dirpath, fname)
    if isinstance(content, str):
        content = content.encode("utf-8", "replace")
    with open(out, "wb") as f:
        f.write(content)

    # Update latest pointer.
    latest = os.path.join(dirpath, f"latest.{ext}")
    try:
        if os.path.islink(latest) or os.path.exists(latest):
            os.unlink(latest)
        os.symlink(fname, latest)
    except OSError:
        # Symlinks not permitted; fall back to copy.
        with open(latest, "wb") as f:
            f.write(content)
    return out


def get_previous_snapshot(source_id: str, extension: str = None) -> str | None:
    """Return path to the most recent prior snapshot for this source,
    excluding the current one. None if none exists."""
    sid = re.sub(r"[^A-Za-z0-9_.-]", "_", source_id)
    dirpath = os.path.join(SNAP_DIR, sid)
    if not os.path.isdir(dirpath):
        return None
    files = []
    for f in os.listdir(dirpath):
        if f.startswith("latest."):
            continue
        if extension and not f.endswith("." + extension):
            continue
        full = os.path.join(dirpath, f)
        if os.path.isfile(full):
            files.append(full)
    if not files:
        return None
    files.sort(key=os.path.getmtime, reverse=True)
    # Skip [0] which is "the most recent" (likely just-saved) and return [1].
    return files[1] if len(files) > 1 else files[0]
