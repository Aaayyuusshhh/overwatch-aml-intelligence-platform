"""Shared scraper-spec resolver for html/pdf/js handlers.

Normalises the various scraper formats found in sources.json into a
(module, callable_name) pair, so all dispatchers behave identically.

Accepted formats (all map to the same call):
    "foo.py"                          -> scrapers.foo, run
    "foo"                             -> scrapers.foo, run
    "scrapers/foo.py"                 -> scrapers.foo, run         (strip prefix)
    "foo.py::bar"                     -> scrapers.foo, bar         (named callable)
    "scrapers/foo.py::bar"            -> scrapers.foo, bar
    "foo.bar"                         -> scrapers.foo, bar         (dotted form)
    "pkg.mod.fn"                      -> scrapers.pkg.mod, fn      (multi-level)

The dotted form is only treated as module+callable when the resolved
module path does NOT exist as a Python package (the prior dispatcher
assumed it did, which is why "friday_us_au_nz_scrapers.scrape_X" failed:
the .py file is a module, not a package). We always try `module.callable`
first; if the import fails as a package, we fall back to splitting off
the trailing component as the callable name.
"""
from __future__ import annotations
import importlib


def resolve(scraper_spec: str):
    """Return (module, callable_name). Raises ImportError / AttributeError
    on failure, so the caller's existing try/except surfaces a clean error."""
    spec = scraper_spec.strip()
    # Normalise the "scrapers/" prefix used in ~18 entries.
    if spec.startswith("scrapers/"):
        spec = spec[len("scrapers/"):]
    # Explicit "::" delimiter for callable name.
    if "::" in spec:
        mod_part, callable_name = spec.split("::", 1)
        callable_name = callable_name.strip() or "run"
    else:
        mod_part, callable_name = spec, "run"
    # Strip trailing ".py" if present.
    if mod_part.endswith(".py"):
        mod_part = mod_part[:-3]
    # First attempt: treat the whole thing as a module path with run/callable.
    try:
        module = importlib.import_module(f"scrapers.{mod_part}")
        return module, callable_name
    except ImportError:
        # Dotted form fallback: "a.b.c" -> try ("a.b", "c"). Only meaningful
        # when no explicit "::" was given and the path has at least one dot.
        if "::" not in scraper_spec and "." in mod_part:
            head, tail = mod_part.rsplit(".", 1)
            module = importlib.import_module(f"scrapers.{head}")
            return module, tail
        raise
