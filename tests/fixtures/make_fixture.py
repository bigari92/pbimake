"""
make_fixture.py — the in-repo regression fixture.

Builds a complete .pbip project from generated data, with no Power BI, no
Windows and no environment variables, then compares every emitted TMDL and
visual.json against the golden copies committed under `golden/`.

    python tests/fixtures/make_fixture.py                # build + compare
    python tests/fixtures/make_fixture.py --write-golden # re-record the goldens
    python tests/fixtures/make_fixture.py --out DIR      # keep the build

WHAT THIS PROVES, AND WHAT IT DOES NOT

The goldens were produced by this repo's own builders. They can only show
that the emitted JSON/TMDL has not *changed* — they cannot show that it is
what Power BI expects, because nothing here ever opened the file. Checking
the output against a Desktop-authored project stays a Windows job.

STABILITY

Every build mints fresh GUIDs (lineageTag, logicalId, page and visual ids)
and bakes the absolute CSV path into the M partition, so a byte comparison
would fail on every run. `normalize_tmdl` / `normalize_visual` below replace
exactly those volatile tokens with placeholders and nothing else: a real
change to any property still shows up as a diff.

Golden files are keyed by the visual's own content (type + title/alt text),
not by its on-disk folder, because the folder name is one of the GUIDs.

The CSV is never committed. `tests/generate_data.py` seeds
`numpy.random.default_rng`, whose PCG64 stream NumPy guarantees across
versions, so the file is reproducible byte-for-byte on any machine — but if a
future NumPy ever does change that stream, the column *types* would shift and
the TMDL golden would be the thing that tells you.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
TESTS = os.path.dirname(HERE)
ROOT = os.path.dirname(TESTS)
sys.path.insert(0, ROOT)

GOLDEN_DIR = os.path.join(HERE, "golden")
CSV_PATH = os.path.join(HERE, "hiring_funnel_clean.csv")

PROJECT = "hiring_funnel"
TABLE = "hiring"
STYLE = "dark_analytics"

# The data generator's window ends here. Pinning it keeps the DAX windows (and
# so the golden TMDL) independent of the day the suite happens to run.
AS_OF = "2026-09-19"

# ── the model ─────────────────────────────────────────────────────────────────
# A representative spread of format strings: counts, currency, percent and a
# one-decimal average. `format_string` is what Power BI renders the number
# with, so each one is worth a golden.
MEASURES = [
    ("Applications", f"COUNTROWS('{TABLE}')", "#,0", None),
    ("Screened", f"CALCULATE(COUNTROWS('{TABLE}'), '{TABLE}'[reached_screen] = 1)",
     "#,0", None),
    ("Interviewed", f"CALCULATE(COUNTROWS('{TABLE}'), '{TABLE}'[reached_interview] = 1)",
     "#,0", None),
    ("Offers", f"CALCULATE(COUNTROWS('{TABLE}'), '{TABLE}'[reached_offer] = 1)",
     "#,0", None),
    ("Hires", f"CALCULATE(COUNTROWS('{TABLE}'), '{TABLE}'[reached_hire] = 1)",
     "#,0", None),
    # The brief's wrong-number case: a true total of EUR 1,740,783 that a card
    # with Auto display units renders as "EUR 2M".
    ("Placement fees", f"SUM('{TABLE}'[placement_fee_eur])", "EUR#,0", None),
    ("Hire rate", "DIVIDE([Hires], [Applications], 0)", "0.0%", None),
    ("Avg days to hire", f"AVERAGE('{TABLE}'[days_to_hire])", "#,0.0",
     "mean days_to_hire over hired applications only"),
    # multi-line DAX, to keep the TMDL block writer covered
    ("Offer acceptance",
     "VAR _offers = [Offers]\nVAR _hires = [Hires]\nRETURN DIVIDE(_hires, _offers, 0)",
     "0.0%", "share of offers that were accepted"),
]


def page_spec(ctx: dict) -> dict:
    """The fixture page: one visual per family the brief asks the net to cover.

    Titles are unique — they are the golden file keys.
    No `icon` and no `background`: both need the Segoe MDL2 Assets glyph font,
    which is Windows-only and deliberately not substituted here (see BRIEF.md).
    """
    return {
        "style": STYLE,
        "theme": True,
        "pages": [{
            "page": 0,
            "canvas": {"width": 1280, "height": 1480},
            "clear": True,
            "visuals": [
                {"type": "card_measure", "title": "Placement fees",
                 "table": TABLE, "measure": "Placement fees",
                 "x": 20, "y": 20, "w": 300, "h": 120},
                {"type": "kpi_tile", "title": "Applications",
                 "table": TABLE, "measure": "Applications",
                 "note_measure": ctx["label"], "note_color_measure": ctx["color"],
                 "trend_col": "applied_month", "trend_measure": ctx["trend"],
                 "x": 340, "y": 20, "w": 300, "h": 180},
                {"type": "bar", "title": "Hires by source",
                 "table": TABLE, "category_col": "source", "measure": "Hires",
                 "sorted_desc": True,
                 "x": 20, "y": 220, "w": 600, "h": 360},
                {"type": "line", "title": "Applications by month",
                 "table": TABLE, "category_col": "applied_month",
                 "measure": "Applications",
                 "x": 640, "y": 220, "w": 620, "h": 360},
                {"type": "funnel", "title": "Hiring funnel",
                 "table": TABLE, "category_col": "stage_reached",
                 "x": 20, "y": 600, "w": 600, "h": 340},
                {"type": "scatter", "title": "Hire rate vs time to hire",
                 "table": TABLE, "x_measure": "Avg days to hire",
                 "y_measure": "Hire rate", "details_col": "recruiter",
                 "x": 640, "y": 600, "w": 620, "h": 340},
                {"type": "treemap", "title": "Applications by department",
                 "table": TABLE, "category_col": "department",
                 "measure": "Applications",
                 "x": 20, "y": 960, "w": 600, "h": 340},
                {"type": "table", "title": "Recruiter scorecard",
                 "table": TABLE, "columns": ["recruiter"],
                 "measures": ["Applications", "Hires", "Hire rate", "Placement fees"],
                 "data_bars": ["Placement fees"],
                 "x": 640, "y": 960, "w": 620, "h": 340},
            ],
        }],
    }


# ── build ─────────────────────────────────────────────────────────────────────

def generate_csv(force: bool = False) -> str:
    """Write the fixture CSV via tests/generate_data.py (seeded, so stable)."""
    if os.path.isfile(CSV_PATH) and not force:
        return CSV_PATH
    env = dict(os.environ, PBIMAKE_FIXTURE_OUT=CSV_PATH)
    subprocess.run([sys.executable, os.path.join(TESTS, "generate_data.py")],
                   check=True, capture_output=True, text=True, env=env)
    return CSV_PATH


def build(out_dir: str) -> dict:
    """Scaffold, model and build the fixture project. Returns the paths dict."""
    import pbip_scaffold
    from design_measures import add_kpi_context
    from pbir_editor import PbirEditor
    from tmdl_editor import TmdlModel

    csv = generate_csv()
    shutil.rmtree(out_dir, ignore_errors=True)
    paths = pbip_scaffold.new_project(out_dir, PROJECT, csv, table_name=TABLE)

    model = TmdlModel(paths["semantic_model"], guard_pbi=False)
    for name, dax, fmt, desc in MEASURES:
        model.add_measure(TABLE, name, dax, format_string=fmt, description=desc,
                          force=True)
    ctx = add_kpi_context(model, TABLE, "Applications", date_col="applied_date",
                          month_col="applied_month", style=STYLE, as_of=AS_OF,
                          force=True)

    editor = PbirEditor(paths["report"], guard_pbi=False)
    editor.build_from_spec(page_spec(ctx), force=True)

    paths["ctx"] = ctx
    paths["csv"] = csv
    return paths


# ── normalization ─────────────────────────────────────────────────────────────

_GUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
                   re.I)
_HEX20 = re.compile(r"\b[0-9a-f]{20}\b")



def normalize_tmdl(text: str) -> str:
    """Strip the tokens that are fresh on every build, and nothing else."""
    text = text.replace("\r\n", "\n")
    text = _GUID.sub("<guid>", text)
    # the M partition bakes in the absolute CSV path (a known gap, README §5)
    text = re.sub(r'File\.Contents\("[^"]*"\)', 'File.Contents("<csv>")', text)
    return text


def normalize_visual(obj: dict) -> dict:
    """Replace the minted visual id; keep every property byte-for-byte."""
    obj = json.loads(json.dumps(obj))
    name = obj.get("name", "")
    # "kt_<10 hex>_value" / "ns_<17 hex>" / a bare 20-hex uid
    name = re.sub(r"^((?:kt|ns)_)[0-9a-f]+", r"\1<id>", name)
    obj["name"] = _HEX20.sub("<id>", name)
    return obj


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (text or "").lower()).strip("_")


def visual_key(obj: dict) -> str:
    """Content-derived golden file name. Stable across builds; the on-disk
    folder name is a GUID and so is not usable as a key."""
    v = obj["visual"]
    vtype = v["visualType"]
    general = (v.get("visualContainerObjects", {}).get("general", [{}])[0]
               .get("properties", {}))
    title = (v.get("visualContainerObjects", {}).get("title", [{}])[0]
             .get("properties", {}).get("text", {}).get("expr", {})
             .get("Literal", {}).get("Value", ""))
    alt = (general.get("altText", {}).get("expr", {})
           .get("Literal", {}).get("Value", ""))
    label = _slug(title.strip("'")) or _slug(alt.strip("'")) or _slug(obj["name"])
    return f"{_slug(vtype)}__{label}"


# ── golden I/O ────────────────────────────────────────────────────────────────

def collect(paths: dict) -> dict:
    """{relative golden path: normalized text} for the whole build."""
    out = {}

    tables_dir = os.path.join(paths["semantic_model"], "definition", "tables")
    for fname in sorted(os.listdir(tables_dir)):
        with open(os.path.join(tables_dir, fname), encoding="utf-8") as fh:
            out[f"tmdl/{fname}"] = normalize_tmdl(fh.read())

    pages_dir = os.path.join(paths["report"], "definition", "pages")
    for page in sorted(d for d in os.listdir(pages_dir)
                       if os.path.isdir(os.path.join(pages_dir, d))):
        vdir = os.path.join(pages_dir, page, "visuals")
        if not os.path.isdir(vdir):
            continue
        for vfolder in sorted(os.listdir(vdir)):
            vpath = os.path.join(vdir, vfolder, "visual.json")
            with open(vpath, encoding="utf-8") as fh:
                obj = normalize_visual(json.load(fh))
            key = f"visuals/{visual_key(obj)}.json"
            if key in out:
                raise RuntimeError(
                    f"two visuals map to the same golden key '{key}' — give "
                    "them distinct titles in page_spec()")
            out[key] = json.dumps(obj, indent=2, sort_keys=True,
                                  ensure_ascii=False) + "\n"
    return out


def write_golden(built: dict) -> None:
    shutil.rmtree(GOLDEN_DIR, ignore_errors=True)
    for rel, text in built.items():
        dest = os.path.join(GOLDEN_DIR, rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
    print(f"wrote {len(built)} golden file(s) to {GOLDEN_DIR}")


def read_golden() -> dict:
    out = {}
    for base, _, files in os.walk(GOLDEN_DIR):
        for f in sorted(files):
            full = os.path.join(base, f)
            rel = os.path.relpath(full, GOLDEN_DIR).replace(os.sep, "/")
            with open(full, encoding="utf-8") as fh:
                out[rel] = fh.read()
    return out


def diff(built: dict, golden: dict) -> list:
    """List of (key, message) for every file that differs. Empty == match."""
    import difflib
    problems = []
    for key in sorted(set(golden) - set(built)):
        problems.append((key, "missing from the build"))
    for key in sorted(set(built) - set(golden)):
        problems.append((key, "not in golden/ — a new visual or table"))
    for key in sorted(set(built) & set(golden)):
        if built[key] != golden[key]:
            d = list(difflib.unified_diff(
                golden[key].splitlines(), built[key].splitlines(),
                "golden", "built", lineterm="", n=1))
            problems.append((key, "\n".join(d[:24])))
    return problems


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--write-golden", action="store_true",
                    help="re-record golden/ from this build")
    ap.add_argument("--out", default=None,
                    help="build directory (default: a temp dir)")
    args = ap.parse_args()

    out_dir = args.out or os.path.join(tempfile.gettempdir(), "pbimake_fixture")
    paths = build(out_dir)
    print(f"built {PROJECT} in {out_dir}")

    built = collect(paths)
    if args.write_golden:
        write_golden(built)
        return 0

    problems = diff(built, read_golden())
    if problems:
        print(f"\n{len(problems)} golden mismatch(es):")
        for key, msg in problems:
            print(f"\n--- {key}\n{msg}")
        return 1
    print(f"golden: {len(built)} file(s) match")
    return 0


if __name__ == "__main__":
    sys.exit(main())
