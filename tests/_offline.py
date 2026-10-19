"""Is the PBIR JSON-schema gate runnable here?

`_validate_pbir.py` fetches Microsoft's published schemas over HTTPS and caches
them in `_schema_cache/`, which is gitignored. On a machine with no route to
developer.microsoft.com and no warm cache, every file reports
"VALIDATOR ERROR: URLError" — a network fact, not a defect in the emitted JSON.

The suites use this to SKIP the schema gate rather than report a false failure
(or, worse, a false pass). A skip is printed and counted separately so it can
never be mistaken for coverage.
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_PROBE = ("https://developer.microsoft.com/json-schemas/fabric/item/report/"
          "definition/visualContainer/2.9.0/schema.json")


def schemas_available() -> bool:
    """True when _validate_pbir can resolve a schema (cache hit or network)."""
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_validate_pbir_probe", os.path.join(ROOT, "_validate_pbir.py"))
    mod = importlib.util.module_from_spec(spec)
    # its report loop AND its summary print run at module level, so blank
    # argv and swallow stdout while importing it
    import contextlib, io
    argv = sys.argv
    sys.argv = argv[:1]
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            spec.loader.exec_module(mod)
    finally:
        sys.argv = argv
    try:
        mod.resolve_schema(_PROBE)
        return True
    except Exception:
        return False


SCHEMA_SKIP_REASON = (
    "PBIR schemas unreachable (no _schema_cache/ and no route to "
    "developer.microsoft.com) — schema gate not exercised"
)
