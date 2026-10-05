# Validate PBIR JSON files against Microsoft's published schemas.
# Usage: python _validate_pbir.py <Report folder> [more Report folders...]
#
# - Schemas are cached on disk (_schema_cache/) so validation works offline.
# - PBI Desktop writes some schema versions Microsoft has NOT published (e.g.
#   visualContainer 2.10.0 -> 404). On 404 we fall back to the newest PUBLISHED
#   version of the same schema family and note it — a PBI-saved project must
#   not fail validation for Microsoft's publishing lag.
import sys, os, json, re, urllib.request, urllib.error

from jsonschema import Draft7Validator
from referencing import Registry, Resource

CACHE = {}
CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_schema_cache")

# newest PUBLISHED version per schema family (fallback for unpublished ones)
PUBLISHED = {
    "visualContainer": "2.9.0",
    "page": "2.1.0",
    "pagesMetadata": "1.1.0",
    "report": "3.3.0",
    "versionMetadata": "1.0.0",
}

def _fetch_raw(uri: str):
    fname = os.path.join(CACHE_DIR, re.sub(r"[^A-Za-z0-9.]+", "_", uri) + ".json")
    if os.path.exists(fname):
        with open(fname, encoding="utf-8") as f:
            return json.load(f)
    req = urllib.request.Request(uri, headers={"User-Agent": "pbir-validator"})
    with urllib.request.urlopen(req, timeout=30) as r:
        doc = json.loads(r.read().decode("utf-8"))
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(fname, "w", encoding="utf-8") as f:
        json.dump(doc, f)
    return doc

_NOTED = set()

def resolve_schema(uri: str):
    """(schema_doc, effective_uri) — 404s fall back to the newest published
    version of the same schema family."""
    if uri in CACHE:
        return CACHE[uri]
    try:
        result = (_fetch_raw(uri), uri)
    except urllib.error.HTTPError as ex:
        if ex.code != 404:
            raise
        m = re.search(r"definition/(\w+)/([\d.]+)/schema.json", uri)
        family = m.group(1) if m else None
        if family not in PUBLISHED or m.group(2) == PUBLISHED[family]:
            raise
        fallback = uri.replace(f"/{m.group(2)}/", f"/{PUBLISHED[family]}/")
        if uri not in _NOTED:
            _NOTED.add(uri)
            print(f"  note: {family} {m.group(2)} not published — validating against {PUBLISHED[family]}")
        result = (_fetch_raw(fallback), fallback)
    CACHE[uri] = result
    return result

def retrieve(uri: str):
    return Resource.from_contents(resolve_schema(uri)[0])

REGISTRY = Registry(retrieve=retrieve)

def validate_file(path: str) -> list:
    with open(path, encoding="utf-8") as f:
        doc = json.load(f)
    schema_uri = doc.get("$schema")
    if not schema_uri:
        return [f"(no $schema declared)"]
    schema, effective = resolve_schema(schema_uri)
    if effective != schema_uri:
        # published schemas pin $schema to their own version (const) — restamp
        # the copy we validate so publishing lag doesn't read as a defect
        doc = {**doc, "$schema": effective}
    validator = Draft7Validator(schema, registry=REGISTRY)
    errs = []
    for e in sorted(validator.iter_errors(doc), key=lambda e: e.json_path):
        errs.append(f"{e.json_path}: {e.message[:300]}")
    return errs

def walk_report(report_folder: str):
    targets = []
    defn = os.path.join(report_folder, "definition")
    for name in ("report.json", "version.json"):
        p = os.path.join(defn, name)
        if os.path.exists(p):
            targets.append(p)
    pages = os.path.join(defn, "pages")
    if os.path.isdir(pages):
        pj = os.path.join(pages, "pages.json")
        if os.path.exists(pj):
            targets.append(pj)
        for pid in sorted(os.listdir(pages)):
            pdir = os.path.join(pages, pid)
            if not os.path.isdir(pdir):
                continue
            targets.append(os.path.join(pdir, "page.json"))
            vdir = os.path.join(pdir, "visuals")
            if os.path.isdir(vdir):
                for vid in sorted(os.listdir(vdir)):
                    targets.append(os.path.join(vdir, vid, "visual.json"))
    return targets

def check_boms(report_folder: str) -> list:
    """
    PBI requires UTF-8 WITHOUT BOM for every text file it reads from the
    project — including StaticResources (a BOM'd theme aborts the whole
    .pbip load with 'Only text with UTF8 encoding without BOM is supported').
    Scans every .json under the Report folder.
    """
    bad = []
    for root, _dirs, files in os.walk(report_folder):
        for name in files:
            if not name.endswith((".json", ".pbir")):
                continue
            p = os.path.join(root, name)
            with open(p, "rb") as f:
                if f.read(3) == b"\xef\xbb\xbf":
                    bad.append(os.path.relpath(p, report_folder))
    return bad

total_bad = 0
for report in sys.argv[1:]:
    print(f"\n===== {report} =====")
    for rel in check_boms(report):
        total_bad += 1
        print(f"  INVALID: {rel} — UTF-8 BOM detected (PBI rejects BOMs; rewrite with encoding='utf-8')")
    for path in walk_report(report):
        rel = os.path.relpath(path, report)
        try:
            errs = validate_file(path)
        except Exception as ex:
            errs = [f"VALIDATOR ERROR: {type(ex).__name__}: {ex}"]
        if errs:
            total_bad += 1
            # identify the visual type for visual.json files
            vt = ""
            try:
                d = json.load(open(path, encoding="utf-8"))
                vt = " [" + d.get("visual", {}).get("visualType", "?") + "]"
            except Exception:
                pass
            print(f"\n  INVALID: {rel}{vt}")
            for e in errs[:6]:
                print(f"    - {e}")
            if len(errs) > 6:
                print(f"    ... and {len(errs) - 6} more")
        else:
            print(f"  ok: {rel}")

print(f"\n{'ALL VALID' if total_bad == 0 else str(total_bad) + ' INVALID FILE(S)'}")
