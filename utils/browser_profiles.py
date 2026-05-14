"""
utils/browser_profiles.py

Persistent browser profile manager for Playwright. Saves and reloads
context state (cookies, localStorage, sessionStorage, IndexedDB) so
later runs can skip cookie banners, login walls (when authorized), and
ASP.NET session-cookie handshakes that take seconds on every fresh
launch (e.g. FCRA which currently sets state cookies before the form
will accept POSTs).

State files live in profiles/<name>/state.json (the standard Playwright
storage_state format). They are plain JSON; safe to inspect, copy,
delete.

Usage:
    from playwright.sync_api import sync_playwright
    from utils.browser_profiles import load_profile, save_profile

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = load_profile(browser, "fcra")
        page = context.new_page()
        # ... drive the page ...
        save_profile(context, "fcra")
        browser.close()

Notes
-----
- load_profile(browser, name) returns a fresh context, hydrated from the
  saved state when one exists, otherwise a clean context with our
  standard UA + ignore_https_errors. It always returns a context — never
  None — so callers don't have to special-case first runs.
- save_profile is best-effort: a failure (corrupt state, permission)
  prints a warning but never raises. We don't want a profile glitch to
  fail an otherwise-successful scrape.
- Profiles do NOT include credentials. Don't store login secrets here.
"""

import json
import os

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROFILES_DIR = os.path.join(PROJECT_ROOT, "profiles")

DEFAULT_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def _state_path(name):
    return os.path.join(PROFILES_DIR, name, "state.json")


def load_profile(browser, name, **context_kwargs):
    """Return a Playwright BrowserContext hydrated from the saved profile
    if it exists; otherwise a fresh context. Extra kwargs are forwarded
    to browser.new_context() so callers can override viewport, locale,
    etc."""
    path = _state_path(name)
    kwargs = dict(
        ignore_https_errors=True,
        user_agent=DEFAULT_UA,
    )
    kwargs.update(context_kwargs)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                json.load(f)  # validate
            kwargs["storage_state"] = path
            print(f"[browser_profiles] loaded {name} from {path}")
        except Exception as e:
            print(f"[browser_profiles] WARN: profile {name} unreadable "
                  f"({type(e).__name__}: {e}); starting fresh")
    return browser.new_context(**kwargs)


def save_profile(context, name):
    """Persist context.storage_state() to profiles/<name>/state.json.
    Failures are logged but not raised."""
    try:
        out = _state_path(name)
        os.makedirs(os.path.dirname(out), exist_ok=True)
        context.storage_state(path=out)
        print(f"[browser_profiles] saved {name} -> {out}")
    except Exception as e:
        print(f"[browser_profiles] WARN: failed to save profile {name}: "
              f"{type(e).__name__}: {e}")


def delete_profile(name):
    """Remove a saved profile. No error if it doesn't exist."""
    out = _state_path(name)
    if os.path.exists(out):
        os.remove(out)
        print(f"[browser_profiles] deleted {out}")


def list_profiles():
    """Return a list of saved profile names."""
    if not os.path.isdir(PROFILES_DIR):
        return []
    return sorted(d for d in os.listdir(PROFILES_DIR)
                  if os.path.exists(_state_path(d)))


if __name__ == "__main__":
    profs = list_profiles()
    print(f"Saved profiles ({len(profs)}):")
    for p in profs:
        size = os.path.getsize(_state_path(p))
        print(f"  {p}  ({size} bytes)")
