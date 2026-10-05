# Headless integration test for pbir_editor v2 + tmdl_editor + pbip_scaffold.
#
# Needs no environment variables, no Power BI and no Windows. Sections 1-2 read
# the in-repo fixture (tests/fixtures/) and compare the build against the golden
# copies committed under tests/fixtures/golden/; sections 3+ scaffold their own
# throwaway project from the same generated CSV and mutate it freely.
#
# WHAT THE GOLDENS PROVE: that the emitted TMDL/visual.json has not CHANGED.
# They were produced by this repo's own builders, so they cannot prove the
# output is what Power BI expects — nothing here ever opened the file. That
# comparison needs a Desktop-authored project on Windows.
import sys, os, json, shutil, tempfile, traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, os.path.join(HERE, "fixtures"))
from pbir_editor import PbirEditor, STATUS_COLORS
from tmdl_editor import TmdlModel
import pbip_scaffold
import make_fixture
from _offline import SCHEMA_SKIP_REASON, schemas_available

PASS, FAIL, SKIP = [], [], []

TBL = make_fixture.TABLE   # the fixture's model table name
N_COLS = 15                # columns in the generated CSV (tests/generate_data.py)

def glob_visual(report_dir):
    """Path of the first visual.json in a report (single-visual test pages)."""
    import glob as _g
    return _g.glob(os.path.join(report_dir, "definition", "pages", "*", "visuals",
                                "*", "visual.json"))[0]

def check(label, cond, detail=""):
    (PASS if cond else FAIL).append(label + (f" — {detail}" if detail and not cond else ""))
    print(("  ok   " if cond else "  FAIL ") + label + (f" — {detail}" if detail and not cond else ""))

def skip(label, reason):
    SKIP.append(f"{label} — {reason}")
    print(f"  SKIP {label} — {reason}")

print("== 1. Fixture build matches the committed goldens ==")
FIXDIR = os.path.join(tempfile.gettempdir(), "pbimake_fixture_suite")
fix = make_fixture.build(FIXDIR)

problems = make_fixture.diff(make_fixture.collect(fix), make_fixture.read_golden())
check(f"every golden file matches the fresh build ({len(make_fixture.read_golden())} files)",
      problems == [],
      "; ".join(k for k, _ in problems))
for key, msg in problems:
    print(f"\n--- {key}\n{msg}\n")

# Structural reads over the fixture project — the checks the ECOM comparison
# used to make, now against a project this repo built itself.
ed = PbirEditor(os.path.join(FIXDIR, f"{make_fixture.PROJECT}.Report"), guard_pbi=False)
check("validation auto-detected sibling SemanticModel", ed.validate)
check(f"model index has {TBL}", ed.model_index.has_table(TBL))
check("measure home lookup", ed.model_index.measure_home("Hires") == TBL,
      str(ed.model_index.measure_home("Hires")))
pg = ed.get_page(0)
check("fixture visuals load (10: 8 families, kpi_tile is 3 parts)",
      len(pg.list_visuals()) == 10, str(len(pg.list_visuals())))
check("titles extracted", any(t for _, _, t in pg.list_visuals() if t))
print(ed.summary())

tm = TmdlModel(os.path.join(FIXDIR, f"{make_fixture.PROJECT}.SemanticModel"),
               guard_pbi=False)
check(f"tmdl lists {TBL}", TBL in tm.list_tables(), str(tm.list_tables()))
check("tmdl lists measures (>=9)", len(tm.list_measures(TBL)) >= 9,
      str(tm.list_measures(TBL)))
check(f"tmdl lists columns ({N_COLS})", len(tm.list_columns(TBL)) == N_COLS,
      str(len(tm.list_columns(TBL))))

print("\n== 2. Scaffold a fresh project in temp ==")
TMP = os.path.join(tempfile.gettempdir(), "pbip_scaffold_test")
shutil.rmtree(TMP, ignore_errors=True)
paths = pbip_scaffold.new_project(
    TMP, "test_proj", make_fixture.CSV_PATH, table_name="funnel")
check("pbip exists", os.path.isfile(paths["pbip"]))
check("base theme copied", os.path.isfile(os.path.join(
    paths["report"], "StaticResources", "SharedResources", "BaseThemes", "CY26SU05.json")))

gen = open(os.path.join(paths["semantic_model"], "definition", "tables", "funnel.tmdl"),
           encoding="utf-8").read()
check("date col typed dateTime + Long Date", "formatString: Long Date" in gen)
check("int col typed int64 sum", "dataType: int64" in gen and "summarizeBy: sum" in gen)
check("M partition QuoteStyle.Csv utf-8", "QuoteStyle=QuoteStyle.Csv" in gen and "Encoding=65001" in gen)
check(f"M lists {N_COLS} columns", f"Columns={N_COLS}" in gen)
gen_raw = open(os.path.join(paths["semantic_model"], "definition", "tables", "funnel.tmdl"),
               "rb").read()
check("CRLF endings", b"\r\n" in gen_raw)

print("\n== 3. Measures via tmdl_editor ==")
tm2 = TmdlModel(paths["semantic_model"])
tm2.add_measure("funnel", "Applications", "COUNTROWS(funnel)",
    format_string="#,0")
tm2.add_measure("funnel", "Hires",
    'CALCULATE(COUNTROWS(funnel), funnel[reached_hire] = 1)',
    format_string="#,0")
tm2.add_measure("funnel", "Hire rate",
    "DIVIDE([Hires], [Applications], 0)", format_string="0.0%")
tm2.add_measure("funnel", "Multi Line Test",
    "VAR s = [Applications]\nVAR p = [Hires]\nRETURN DIVIDE(p, s)",
    format_string="0.00%", description="multiline + description test")
check("4 measures listed", len(tm2.list_measures("funnel")) == 4, str(tm2.list_measures("funnel")))
tm2.add_measure("funnel", "Hire rate",
    "DIVIDE([Hires], [Applications])", format_string="0.00%")
check("upsert did not duplicate", len(tm2.list_measures("funnel")) == 4)
import re as _re
_tmdl_path = os.path.join(paths["semantic_model"], "definition", "tables", "funnel.tmdl")
_tag_re = _re.compile(r"measure 'Hire rate'.*?lineageTag: ([0-9a-f-]+)", _re.S)
tag_before = _tag_re.search(open(_tmdl_path, encoding="utf-8").read()).group(1)
tm2.add_measure("funnel", "Hire rate",
    "DIVIDE([Hires], [Applications])", format_string="0.00%")
tag_after = _tag_re.search(open(_tmdl_path, encoding="utf-8").read()).group(1)
check("lineageTag preserved on measure update", tag_before == tag_after,
      f"{tag_before} -> {tag_after}")
tm2.remove_measure("funnel", "Multi Line Test")
check("remove works", len(tm2.list_measures("funnel")) == 3)
body = open(os.path.join(paths["semantic_model"], "definition", "tables", "funnel.tmdl"),
            encoding="utf-8").read()
check("formatString written", "formatString: 0.00%" in body)
check("measure before columns",
      body.index("measure 'Hire rate'") < body.index("column application_id"))

print("\n== 4. Validation negative tests ==")
ed2 = PbirEditor(paths["report"])
check("validation on for scaffolded project", ed2.validate)
pg2 = ed2.get_page(0)
try:
    pg2.add_bar_chart("x", table="funnel", category_col="sorce", value_col="seniority")
    check("typo column rejected", False)
except ValueError as e:
    check("typo column rejected", "source" in str(e), str(e))
try:
    pg2.add_card_measure("x", table="funnel", measure="Aplications")
    check("typo measure rejected", False)
except ValueError as e:
    check("typo measure rejected", "Applications" in str(e), str(e))
try:
    pg2.add_bar_chart("x", table="funel", category_col="source")
    check("typo table rejected", False)
except ValueError as e:
    check("typo table rejected", "funnel" in str(e), str(e))
pg2.clear_visuals()

print("\n== 5. Spec build with every visual family ==")
spec = {
  "style": "dark_analytics",
  "theme": True,
  "pages": [{
    "page": 0,
    "canvas": {"width": 1280, "height": 1380},
    "visuals": [
      {"type": "text_box", "text": "Hiring Funnel — Test Build", "x": 20, "y": 10, "w": 800, "h": 45, "font_size": 20},
      {"type": "kpi_card", "title": "Applications", "table": "funnel", "measure": "Applications", "x": 20, "y": 70, "w": 390, "h": 120},
      {"type": "kpi_card", "title": "Hire rate", "table": "funnel", "measure": "Hire rate", "x": 430, "y": 70, "w": 390, "h": 120},
      {"type": "slicer", "table": "funnel", "column": "department", "x": 840, "y": 70, "w": 200, "h": 60},
      {"type": "slicer", "table": "funnel", "column": "applied_date", "slicer_type": "between", "x": 1060, "y": 70, "w": 200, "h": 60},
      {"type": "bar_chart", "title": "Applications by Source", "table": "funnel", "category_col": "source", "measure": "Applications", "sorted_desc": True, "x": 20, "y": 210, "w": 590, "h": 400},
      {"type": "column_chart", "title": "Applications by Department", "table": "funnel", "category_col": "department", "measure": "Applications", "x": 630, "y": 210, "w": 590, "h": 400,
       "series_colors": {"Engineering": "#00B4D8", "QA": "#F72585", "Design": "#FFBE0B"}},
      {"type": "funnel", "title": "Conversion Funnel", "table": "funnel", "category_col": "stage_reached", "x": 20, "y": 630, "w": 590, "h": 350},
      {"type": "donut", "title": "Applications by Seniority", "table": "funnel", "category_col": "seniority", "measure": "Applications", "x": 630, "y": 630, "w": 280, "h": 350},
      {"type": "line", "title": "Applications Trend", "table": "funnel", "category_col": "applied_date", "measure": "Applications", "x": 20, "y": 1000, "w": 1240, "h": 350},
      {"type": "scatter", "title": "Applications vs Hires", "table": "funnel", "x_measure": "Applications", "y_measure": "Hires", "details_col": "location", "x": 930, "y": 630, "w": 310, "h": 350},
      {"type": "matrix", "title": "Matrix Test", "table": "funnel", "row_cols": ["source"], "col_cols": ["seniority"], "value_measures": ["Applications"], "x": 20, "y": 2170, "w": 600, "h": 220},
      {"type": "table", "title": "Table Test", "table": "funnel", "columns": ["location", "recruiter"], "measures": ["Applications"], "x": 20, "y": 1360, "w": 500, "h": 200},
      {"type": "gauge", "title": "Gauge Test", "table": "funnel", "value_measure": "Hire rate", "min_val": 0, "max_val": 1, "x": 540, "y": 1360, "w": 250, "h": 200},
      {"type": "combo", "title": "Combo Test", "table": "funnel", "category_col": "department", "bar_measure": "Applications", "line_measure": "Hire rate", "x": 810, "y": 1360, "w": 430, "h": 200},
      {"type": "stacked_bar_chart", "title": "Stacked Test", "table": "funnel", "category_col": "source", "series_col": "department", "measure": "Applications", "x": 20, "y": 1580, "w": 590, "h": 300},
      {"type": "treemap", "title": "Treemap Test", "table": "funnel", "category_col": "location", "measure": "Applications", "x": 630, "y": 1580, "w": 400, "h": 300},
      {"type": "waterfall", "title": "Waterfall Test", "table": "funnel", "category_col": "department", "measure": "Applications", "x": 1050, "y": 1580, "w": 190, "h": 300},
      {"type": "area", "title": "Area Test", "table": "funnel", "category_col": "applied_date", "measure": "Applications", "x": 20, "y": 1900, "w": 590, "h": 250},
      {"type": "pie", "title": "Pie Test", "table": "funnel", "category_col": "seniority", "measure": "Applications", "x": 630, "y": 1900, "w": 300, "h": 250},
      {"type": "multi_row_card", "title": "MRC Test", "table": "funnel", "measures": ["Applications", "Hires"], "x": 950, "y": 1900, "w": 290, "h": 250}
    ]
  }]
}
print(ed2.build_from_spec(spec))

vdir_root = os.path.join(paths["report"], "definition", "pages")
pgfolders = [d for d in os.listdir(vdir_root) if os.path.isdir(os.path.join(vdir_root, d))]
vdir = os.path.join(vdir_root, pgfolders[0], "visuals")
n_visuals = len(os.listdir(vdir))
check("21 visuals written", n_visuals == 21, str(n_visuals))

# every visual.json parses + encoding spot checks
types_seen, has_dD, has_solid = set(), False, False
for d in os.listdir(vdir):
    v = json.load(open(os.path.join(vdir, d, "visual.json"), encoding="utf-8"))
    types_seen.add(v["visual"]["visualType"])
    blob = json.dumps(v)
    if '"9D"' in blob: has_dD = True
    if '"solid"' in blob: has_solid = True
check("fontSize uses D suffix", has_dD)
check("colors solid-wrapped", has_solid)
check("classic card used", "card" in types_seen, str(sorted(types_seen)))
check("slicer + textbox present", {"slicer", "textbox"} <= types_seen)
check("stacked types use core names", "barChart" in types_seen and "stackedBarChart" not in types_seen, str(sorted(types_seen)))

# funnel must carry a value-desc sort (PBI defaults to alphabetical stage order)
funnel_sorted = False
for d in os.listdir(vdir):
    v = json.load(open(os.path.join(vdir, d, "visual.json"), encoding="utf-8"))
    if v["visual"]["visualType"] == "funnel":
        sd = v["visual"]["query"].get("sortDefinition", {})
        funnel_sorted = sd.get("sort", [{}])[0].get("direction") == "Descending"
check("funnel sorted by value desc", funnel_sorted)

# gauge min/max must live in objects.axis, never as Literal projections
gauge_ok = False
for d in os.listdir(vdir):
    v = json.load(open(os.path.join(vdir, d, "visual.json"), encoding="utf-8"))
    if v["visual"]["visualType"] == "gauge":
        qs_blob = json.dumps(v["visual"]["query"]["queryState"])
        gauge_ok = '"Literal"' not in qs_blob and "axis" in v["visual"].get("objects", {})
check("gauge min/max in objects.axis (no Literal projections)", gauge_ok)

check("no overlapping data visuals", ed2.get_page(0).find_overlaps() == [],
      str(ed2.get_page(0).find_overlaps()))

pj = json.load(open(os.path.join(vdir_root, pgfolders[0], "page.json"), encoding="utf-8"))
check("canvas auto-grew beyond 1380 (content to 2150)", pj["height"] >= 2150, str(pj["height"]))

rj = json.load(open(os.path.join(paths["report"], "definition", "report.json"), encoding="utf-8"))
check("custom theme registered", rj.get("themeCollection", {}).get("customTheme", {}).get("name") == "professional_dark_analytics.json", json.dumps(rj.get("themeCollection", {})))
check("theme file in RegisteredResources", os.path.isfile(os.path.join(
    paths["report"], "StaticResources", "RegisteredResources", "professional_dark_analytics.json")))

print("\n== 6. Non-destructive reload + selective ops ==")
ed3 = PbirEditor(paths["report"])
pg3 = ed3.get_page(0)
check("reload sees 21 visuals", len(pg3.list_visuals()) == 21, str(len(pg3.list_visuals())))
check("remove by title", pg3.remove_visual("Pie Test"))
pg3.add_card_measure("Apostrophe's Card", table="funnel", measure="Applications",
                     x=630, y=1900, w=300, h=250, style="dark_analytics")
pg3.save()
ed4 = PbirEditor(paths["report"])
pg4 = ed4.get_page(0)
titles = [t for _, _, t in pg4.list_visuals()]
check("still 21 after swap", len(titles) == 21, str(len(titles)))
check("apostrophe title round-trips", "Apostrophe's Card" in titles, str(titles[-3:]))
check("pie removed", "Pie Test" not in titles)

print("\n== 7. add_page / remove_page ==")
p2 = ed4.add_page("Detail Page", width=1280, height=720)
p2.add_table("Detail", table="funnel", columns=["location"], measures=["Applications"],
             x=20, y=20, w=600, h=400, style="dark_analytics")
p2.save()
meta = json.load(open(os.path.join(vdir_root, "pages.json"), encoding="utf-8"))
check("pages.json has 2 pages", len(meta["pageOrder"]) == 2, str(meta))
check("get_page by new name", ed4.get_page("Detail Page").display_name == "Detail Page")
ed4.remove_page("Detail Page")
meta = json.load(open(os.path.join(vdir_root, "pages.json"), encoding="utf-8"))
check("page removed from pages.json", len(meta["pageOrder"]) == 1)

print("\n== 8. Competition background generator (Phase 1) ==")
pg8 = ed4.get_page(0)
n_before = len(pg8.list_visuals())
pg8.generate_background(header_title="Hiring Funnel Performance",
                        header_subtitle="Applications, conversion and department mix — test build",
                        style="dark_analytics", nav_rail=56)
pg8.save()

res_dir8 = os.path.join(paths["report"], "StaticResources", "RegisteredResources")
bg_files = [f for f in os.listdir(res_dir8) if f.startswith("bg_") and f.endswith(".png")]
check("background PNG written", len(bg_files) == 1, str(bg_files))

from PIL import Image as _Img
pj8 = json.load(open(os.path.join(vdir_root, pgfolders[0], "page.json"), encoding="utf-8"))
with _Img.open(os.path.join(res_dir8, bg_files[0])) as im8:
    check("PNG size matches canvas", im8.size == (pj8["width"], pj8["height"]),
          f"{im8.size} vs {pj8['width']}x{pj8['height']}")

bg_obj = pj8.get("objects", {}).get("background", [{}])[0].get("properties", {})
item = (bg_obj.get("image", {}).get("image", {}).get("url", {})
        .get("expr", {}).get("ResourcePackageItem", {}))
check("page.json background wired", item.get("ItemName") == bg_files[0]
      and item.get("PackageName") == "RegisteredResources", json.dumps(item))

rj8 = json.load(open(os.path.join(paths["report"], "definition", "report.json"), encoding="utf-8"))
reg8 = next(p for p in rj8["resourcePackages"] if p["type"] == "RegisteredResources")
check("image registered in report.json (type Image)",
      any(i["name"] == bg_files[0] and i["type"] == "Image" for i in reg8["items"]))
check("theme registration survived image registration",
      any(i.get("type") == "CustomTheme" for i in reg8["items"]))

# all data visuals flipped to transparent
transparent, opaque = 0, 0
for d in os.listdir(vdir):
    v = json.load(open(os.path.join(vdir, d, "visual.json"), encoding="utf-8"))
    bgp = (v["visual"].get("visualContainerObjects", {}).get("background", [{}])[0]
           .get("properties", {}).get("show", {}).get("expr", {})
           .get("Literal", {}).get("Value"))
    if bgp == "false":
        transparent += 1
    else:
        opaque += 1
check("all visuals transparent over background", opaque == 0,
      f"{opaque} opaque / {transparent} transparent")
check("visual count unchanged", len(pg8.list_visuals()) == n_before)

# header shift happened for slot visuals (design-layer visuals may stay in the band)
from pbir_editor import NO_SLOT_TYPES
pg8b = PbirEditor(paths["report"]).get_page(0)
min_y8 = min(v["position"]["y"] for v in pg8b._visuals
             if v["visual"]["visualType"] not in NO_SLOT_TYPES)
check("slot visuals shifted below header band", min_y8 >= 100, str(min_y8))

print("\n== 9. Design-layer visuals (Phase 2) ==")
pg9 = PbirEditor(paths["report"]).get_page(0)
n9 = len(pg9.list_visuals())
pg9.add_shape(shape="rectangleRounded", fill="#F72585", x=20, y=2400, w=300, h=8)
# add_icon() needs the Segoe MDL2 Assets glyph font, which is Windows-only and
# deliberately NOT substituted (BRIEF.md: a substitute renders tofu boxes). Two
# plain PNGs exercise the same add_image() path — resource registration and the
# image visual's encoding — without pretending the glyph render was checked.
skip("add_icon glyph render", "Segoe MDL2 Assets absent — icon PNGs not rendered")
_png_dir = os.path.join(TMP, "pngs")
os.makedirs(_png_dir, exist_ok=True)
from PIL import Image as _PImg
_pngs = []
for _i, _rgb in enumerate([(247, 37, 133), (255, 190, 11)]):
    _pp = os.path.join(_png_dir, f"probe_{_i}.png")
    _PImg.new("RGBA", (32, 32), _rgb + (255,)).save(_pp)
    _pngs.append(_pp)
pg9.add_image(_pngs[0], scaling="Fit", x=340, y=2400, w=32, h=32)
pg9.add_image(_pngs[1], scaling="Fit", x=390, y=2400, w=32, h=32)
pg9.add_page_navigator(x=440, y=2400, w=300, h=40)
pg9.save()

ed9 = PbirEditor(paths["report"])
pg9b = ed9.get_page(0)
types9 = {vt for _, vt, _ in pg9b.list_visuals()}
check("design visuals written (+4)", len(pg9b.list_visuals()) == n9 + 4,
      str(len(pg9b.list_visuals())))
check("shape/image/pageNavigator types present",
      {"shape", "image", "pageNavigator"} <= types9, str(sorted(types9)))

shape_v = next(v for v in pg9b._visuals if v["visual"]["visualType"] == "shape")
fill = shape_v["visual"]["objects"]["fill"][0]
check("shape fill has default-state selector", fill.get("selector", {}).get("id") == "default")
check("shape tileShape encoded", "rectangleRounded" in json.dumps(shape_v["visual"]["objects"]["shape"]))

img_v = next(v for v in pg9b._visuals if v["visual"]["visualType"] == "image")
img_item = (img_v["visual"]["objects"]["image"][0]["properties"]["sourceFile"]["image"]
            ["url"]["expr"]["ResourcePackageItem"])
check("image references RegisteredResources", img_item.get("PackageName") == "RegisteredResources")
rj9 = json.load(open(os.path.join(paths["report"], "definition", "report.json"), encoding="utf-8"))
reg9 = next(p for p in rj9["resourcePackages"] if p["type"] == "RegisteredResources")
png_items = [i for i in reg9["items"] if i["name"].startswith("probe_")]
check("image PNGs registered in report.json (2)", len(png_items) == 2, str(png_items))
check("image files exist", all(os.path.isfile(os.path.join(
    paths["report"], "StaticResources", "RegisteredResources", i["name"])) for i in png_items))
check("design visuals excluded from overlap check", pg9b.find_overlaps() == [],
      str(pg9b.find_overlaps()))

print("\n== 10. Layout templates (Phase 3) ==")
# Glyph icons are left out of the template specs: add_icon() needs Segoe MDL2
# Assets (Windows-only, deliberately not substituted — BRIEF.md). What the
# templates place icons *at* is therefore unverified here; the grid, bounds and
# overlap checks below still run.
skip("template glyph icon placement", "Segoe MDL2 Assets absent")

from layout_templates import executive_hero, sidebar_nav, story_page, CANVAS_W, MARGIN

spec_hero = executive_hero(
    style="dark_analytics", table="funnel",
    header={"title": "Template Test — Executive Hero", "subtitle": "layout smoke test"},
    kpis=[{"title": "Applications", "measure": "Applications"},
          {"title": "Hires", "measure": "Hires"},
          {"title": "Hire rate", "measure": "Hire rate"}],
    slicer={"column": "applied_date", "slicer_type": "between"},
    hero={"type": "funnel", "title": "Conversion Funnel", "category_col": "stage_reached",
          "value_col": "stage_reached", "agg": "count"},
    supporting=[{"type": "column", "title": "By Device", "category_col": "department",
                 "measure": "Applications"},
                {"type": "donut", "title": "User Mix", "category_col": "seniority",
                 "measure": "Applications"}],
    detail=[{"type": "bar", "title": "Top Sources", "category_col": "source",
             "measure": "Applications", "sorted_desc": True},
            {"type": "line", "title": "Trend", "category_col": "applied_date",
             "measure": "Applications"}],
    nav=True)
ed10 = PbirEditor(paths["report"])
ed10.build_from_spec(spec_hero)

spec_side = sidebar_nav(
    style="dark_analytics", table="funnel",
    header={"title": "Template Test — Sidebar", "subtitle": "rail layout"},
    kpis=[{"title": "Applications", "measure": "Applications"},
          {"title": "Hire rate", "measure": "Hire rate"}],
    charts=[{"type": "bar", "title": "Sources", "category_col": "source", "measure": "Applications", "sorted_desc": True},
            {"type": "column", "title": "Devices", "category_col": "department", "measure": "Applications"},
            {"type": "line", "title": "Trend", "category_col": "applied_date", "measure": "Applications"}],
    page="new:Sidebar")
ed10.build_from_spec(spec_side)

spec_story = story_page(
    style="dark_analytics", table="funnel",
    header={"title": "Template Test — Story", "subtitle": "narrative layout"},
    hero_kpi={"title": "Hire rate", "measure": "Hire rate"},
    narrative="Only 5.2% of applications end in a hire.\nThe tightest stage is interview to offer.",
    charts=[{"type": "funnel", "title": "Where Sessions Drop Off", "category_col": "stage_reached",
             "value_col": "stage_reached", "agg": "count"}],
    page="new:Story")
ed10.build_from_spec(spec_story)

edv = PbirEditor(paths["report"])
check("3 template pages built", len(edv._page_folders()) == 3, str(len(edv._page_folders())))

for pkey, expect_nav in ((0, True), ("Sidebar", True), ("Story", False)):
    pgt = edv.get_page(pkey)
    types_t = {vt for _, vt, _ in pgt.list_visuals()}
    check(f"page {pkey}: no overlaps", pgt.find_overlaps() == [], str(pgt.find_overlaps()))
    in_bounds = all(v["position"]["x"] >= 0 and
                    v["position"]["x"] + v["position"]["width"] <= CANVAS_W
                    for v in pgt._visuals)
    check(f"page {pkey}: all visuals within canvas width", in_bounds)
    if expect_nav:
        check(f"page {pkey}: page navigator present", "pageNavigator" in types_t)

pg_hero = edv.get_page(0)
hero_v = next(v for v in pg_hero._visuals if v["visual"]["visualType"] == "funnel")
supp_v = next(v for v in pg_hero._visuals if v["visual"]["visualType"] == "clusteredColumnChart")
check("hero is larger than supporting (visual hierarchy)",
      hero_v["position"]["width"] * hero_v["position"]["height"] >
      2 * supp_v["position"]["width"] * supp_v["position"]["height"])
try:
    executive_hero(style="dark_analytics", table="funnel", header={}, kpis=[],
                   hero={"type": "bar", "title": "x", "category_col": "source"})
    check("kpi count validated", False)
except ValueError:
    check("kpi count validated", True)

print("\n== 11. Validator: unpublished-schema fallback + BOM detection ==")
import importlib.util as _ilu
_spec = _ilu.spec_from_file_location(
    "_validate_pbir", os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "_validate_pbir.py"))
vp = _ilu.module_from_spec(_spec)
_argv, sys.argv = sys.argv, sys.argv[:1]   # its report loop walks sys.argv[1:]
try:
    _spec.loader.exec_module(vp)
finally:
    sys.argv = _argv

# a real visual restamped with PBI's unpublished 2.10.0 schema must still validate
if schemas_available():
    any_page = [d for d in os.listdir(vdir_root) if os.path.isdir(os.path.join(vdir_root, d))][0]
    any_vdir = os.path.join(vdir_root, any_page, "visuals")
    sample = json.load(open(os.path.join(any_vdir, os.listdir(any_vdir)[0], "visual.json"), encoding="utf-8"))
    sample["$schema"] = sample["$schema"].replace("2.9.0", "2.10.0")
    tmp210 = os.path.join(TMP, "visual_210.json")
    json.dump(sample, open(tmp210, "w", encoding="utf-8"))
    errs210 = vp.validate_file(tmp210)
    check("2.10.0 schema falls back to published 2.9.0", errs210 == [], str(errs210)[:200])
else:
    skip("2.10.0 schema falls back to published 2.9.0", SCHEMA_SKIP_REASON)

bom_path = os.path.join(TMP, "bom_probe.Report", "definition", "x.json")
os.makedirs(os.path.dirname(bom_path), exist_ok=True)
open(bom_path, "wb").write(b"\xef\xbb\xbf{}")
check("BOM scanner catches BOM files", vp.check_boms(os.path.join(TMP, "bom_probe.Report")) != [])

print("\n== 12. Multi-table: relationships, Top-N, alt text (Tier 1) ==")
import pandas as _pd
STAR = os.path.join(tempfile.gettempdir(), "pbip_star_suite")
shutil.rmtree(STAR, ignore_errors=True)
os.makedirs(STAR)
_pd.DataFrame({"pid": [1, 2, 3], "cat": ["A", "B", "C"]}).to_csv(
    os.path.join(STAR, "dim.csv"), index=False)
_pd.DataFrame({"pid": [1, 1, 2, 3], "rev": [10.0, 20.0, 5.0, 1.0]}).to_csv(
    os.path.join(STAR, "fact.csv"), index=False)

spaths = pbip_scaffold.new_project(STAR, "star", os.path.join(STAR, "fact.csv"),
                                   extra_csvs=[os.path.join(STAR, "dim.csv")])
check("scaffold returns both tables", spaths["tables"] == ["fact", "dim"], str(spaths["tables"]))

stm = TmdlModel(spaths["semantic_model"])
stm.add_relationship("fact", "pid", "dim", "pid")
stm.add_relationship("fact", "pid", "dim", "pid")   # duplicate -> skipped
check("relationship written once", len(stm.list_relationships()) == 1, str(stm.list_relationships()))
check("relationships.tmdl exists", os.path.isfile(os.path.join(
    spaths["semantic_model"], "definition", "relationships.tmdl")))
stm.add_measure("fact", "Total Rev", "SUM(fact[rev])", format_string="$#,0")

sed = PbirEditor(spaths["report"])
check("model index sees both tables", {"fact", "dim"} <= set(sed.model_index.tables))
spg = sed.get_page(0)
spg.add_bar_chart("Top Categories", table="dim", category_col="cat",
                  measure=("fact", "Total Rev"), top_n=2,
                  x=20, y=20, w=600, h=300)
try:
    spg.add_bar_chart("bad", table="dim", category_col="cat",
                      value_col="cat", agg="count", top_n=2)
    check("top_n without measure rejected", False)
except ValueError as e:
    check("top_n without measure rejected", "measure" in str(e))
spg.save()

sv = json.load(open(glob_visual(spaths["report"]), encoding="utf-8"))
fc = sv.get("filterConfig", {}).get("filters", [{}])[0]
check("filterConfig type TopN", fc.get("type") == "TopN")
check("Top-N count encoded", fc.get("filter", {}).get("From", [{}])[0]
      .get("Expression", {}).get("Subquery", {}).get("Query", {}).get("Top") == 2)
blob = json.dumps(sv)
check("cross-table aliases in subquery", '"Entity": "dim"' in blob and '"Entity": "fact"' in blob)
alt = (sv["visual"].get("visualContainerObjects", {}).get("general", [{}])[0]
       .get("properties", {}).get("altText", {}).get("expr", {}).get("Literal", {}).get("Value"))
check("auto alt text generated", alt == "'Bar chart: Top Categories'", str(alt))
spg2 = PbirEditor(spaths["report"]).get_page(0)
check("set_alt_text overrides", spg2.set_alt_text("Top Categories", "Custom narration"))

# data bars on tables (ground-truthed encoding: columnFormatting + metadata selector)
spg2.add_table("Detail", table="dim", columns=["cat"],
               measures=[("fact", "Total Rev")],
               data_bars=[("fact", "Total Rev")], x=20, y=340, w=600, h=200)
spg2.save()
tblv = next(v for v in PbirEditor(spaths["report"]).get_page(0)._visuals
            if v["visual"]["visualType"] == "tableEx")
cf = tblv["visual"]["objects"]["columnFormatting"][0]
check("data bars selector uses queryRef metadata",
      cf["selector"] == {"metadata": "fact.Total Rev"}, str(cf.get("selector")))
check("data bars use theme tokens",
      "'maxColor'" in json.dumps(cf["properties"]["dataBars"]))

print("\n" + "=" * 60)
print(f"PASS: {len(PASS)}   FAIL: {len(FAIL)}   SKIP: {len(SKIP)}")
if SKIP:
    print("SKIPPED (not coverage — needs Windows fonts or network):")
    for s_ in SKIP:
        print("  - " + s_)
if FAIL:
    print("FAILED:")
    for f in FAIL:
        print("  - " + f)
    sys.exit(1)
print("ALL TESTS PASSED")
