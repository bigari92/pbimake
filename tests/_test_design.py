# Headless tests for the design layer: KPI tiles, highlight colors, icon tiles,
# design_measures DAX, theme fonts. Builds a throwaway project in %TEMP%.
import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import pbip_scaffold
from design_measures import add_highlight_color, add_kpi_context
from layout_templates import KPI_TILE_H, executive_hero
from pbir_editor import STYLES, THEMES_DIR, PbirEditor
from tmdl_editor import TmdlModel

PASS, FAIL = [], []


def check(label, cond, detail=""):
    (PASS if cond else FAIL).append(label)
    print(("  ok   " if cond else "  FAIL ") + label + (f" — {detail}" if detail and not cond else ""))


print("== 1. Theme fonts: no missing 'DIN' face ==")
themes = glob.glob(os.path.join(THEMES_DIR, "professional_*.json"))
check("built-in theme files found", {"professional_corporate_light.json",
      "professional_dark_analytics.json", "professional_minimal_neutral.json"}
      <= {os.path.basename(t) for t in themes}, str(themes))
for t in themes:
    txt = open(t, encoding="utf-8").read()
    check(f"{os.path.basename(t)} has no DIN", '"DIN"' not in txt)

print("\n== 2. Scaffold a project with a date + month column ==")
TMP = os.path.join(tempfile.gettempdir(), "pbip_design_suite")
shutil.rmtree(TMP, ignore_errors=True)
os.makedirs(TMP)
csv = os.path.join(TMP, "sales.csv")
with open(csv, "w", encoding="utf-8") as f:
    f.write("order_id,order_date,order_month,region,revenue\n")
    for i in range(1, 91):
        m = 1 + (i % 6)
        f.write(f"o{i},2026-0{m}-{1 + i % 27:02d},2026-0{m}-01,"
                f"{['North', 'South', 'East', 'West'][i % 4]},{100 + i}\n")
paths = pbip_scaffold.new_project(TMP, "design", csv)
m = TmdlModel(paths["semantic_model"], guard_pbi=False)
m.add_measure("sales", "Total revenue", "SUM('sales'[revenue])", format_string="$#,0",
              force=True)
m.add_measure("sales", "Orders placed", "COUNTROWS('sales')", format_string="#,0",
              force=True)

print("\n== 3. design_measures ==")
ctx = add_kpi_context(m, "sales", "Total revenue", date_col="order_date",
                      month_col="order_month", style="dark_analytics", force=True)
check("context returns 4 names", set(ctx) == {"change", "label", "color", "trend"}, str(ctx))
measures = m.list_measures("sales")
for k, name in ctx.items():
    check(f"measure '{name}' written", name in measures)
tf = m._table_file("sales")
body = "\n".join(tf.lines)
check("comparison uses complete months (EOMONTH)", "EOMONTH(_lastDay, -1)" in body)
check("prior-period coverage guard", "_covered" in body)
check("no INT() on dates", "INT(_" not in body)
check("label arrows present", "▲" in body and "▼" in body)
s = STYLES["dark_analytics"]
check("color measure uses style positive/negative",
      s["positive"] in body and s["negative"] in body)
check("text measures carry no formatString",
      "change label" in body and "change color" in body)

ctx2 = add_kpi_context(m, "sales", "Orders placed", date_col="order_date",
                       compare="yoy", compare_as="points", higher_is_better=False,
                       style="dark_analytics", force=True)
check("yoy/points: no trend without month_col", "trend" not in ctx2)
blk = tf  # reload after write
body2 = "\n".join(m._table_file("sales").lines)
check("points label says pts", '" pts"' in body2)
check("yoy window uses EDATE -12", "EDATE(_curStart, -12)" in body2)

hl = add_highlight_color(m, "sales", "Total revenue", "region",
                         style="dark_analytics", force=True)
body3 = "\n".join(m._table_file("sales").lines)
check("highlight measure written", hl in m.list_measures("sales"))
check("highlight uses accent + muted", s["accent"] in body3 and s["muted"] in body3)
check("highlight ranks over ALLSELECTED", "ALLSELECTED('sales'[region])" in body3)
try:
    add_highlight_color(m, "sales", "Total revenue", "region", direction="sideways")
    check("bad direction rejected", False)
except ValueError:
    check("bad direction rejected", True)

print("\n== 4. KPI tiles + highlight via the executive_hero template ==")
spec = executive_hero(
    style="dark_analytics", table="sales",
    header={"title": "Where does revenue come from?", "subtitle": "test"},
    kpis=[{"title": "Revenue", "measure": "Total revenue", "icon": "money",
           "note_measure": ctx["label"], "note_color_measure": ctx["color"],
           "trend_col": "order_month", "trend_measure": ctx["trend"]},
          {"title": "Orders", "measure": "Orders placed", "icon": "cart",
           "trend_col": "order_month"}],
    hero={"type": "bar_chart", "title": "North leads", "category_col": "region",
          "measure": "Total revenue", "color_measure": hl})
kvis = [v for v in spec["pages"][0]["visuals"] if v["type"] == "kpi_tile"]
check("template emits kpi_tile for rich KPIs", len(kvis) == 2)
check("tile row height", all(v["h"] == KPI_TILE_H for v in kvis))
check("tile index assigned", [v["index"] for v in kvis] == [0, 1])
hero = [v for v in spec["pages"][0]["visuals"] if v["type"] == "bar_chart"][0]
check("hero starts below the taller KPI row", hero["y"] >= 110 + KPI_TILE_H + 20, str(hero["y"]))

ed = PbirEditor(paths["report"], guard_pbi=False)
ed.build_from_spec(spec)
vfiles = glob.glob(os.path.join(paths["report"], "definition", "pages", "*", "visuals",
                                "*", "visual.json"))
vis = [json.load(open(f, encoding="utf-8")) for f in vfiles]
tiles = [v for v in vis if v["name"].startswith("kt_")]
check("tile parts written (value+note+trend, value+trend)", len(tiles) == 5,
      str([v["name"] for v in tiles]))
gids = {v["name"].split("_")[1] for v in tiles}
check("two tile groups", len(gids) == 2)
note = [v for v in tiles if v["name"].endswith("_note")][0]
col = note["visual"]["objects"]["labels"][0]["properties"]["color"]
check("note color is measure-driven (fx)",
      col["solid"]["color"]["expr"]["Measure"]["Property"] == ctx["color"])
trend = [v for v in tiles if v["name"].endswith("_trend")][0]
check("trend is an area chart without axes",
      trend["visual"]["visualType"] == "areaChart"
      and trend["visual"]["objects"]["categoryAxis"][0]["properties"]["show"]
      ["expr"]["Literal"]["Value"] == "false")
sels = {t["visual"]["objects"]["dataPoint"][0].get("selector", {}).get("metadata")
        for t in tiles if t["name"].endswith("_trend")}
check("trend series color keyed to the field queryRef",
      sels == {f"sales.{ctx['trend']}", "sales.Orders placed"}, str(sels))
check("trend strip tall enough to render (>=50px)", trend["position"]["height"] >= 50,
      str(trend["position"]["height"]))
for part in tiles:
    vco = part["visual"]["visualContainerObjects"]
    if vco.get("dropShadow", [{}])[0].get("properties", {}).get("show", {}) \
            .get("expr", {}).get("Literal", {}).get("Value") != "false":
        check(f"tile part {part['name']} has shadow off", False)
        break
else:
    check("every tile part has its drop shadow off", True)
try:
    ed.get_page(0).add_kpi_tile("x", "sales", "Total revenue", note_measure=ctx["label"],
                                trend_col="order_month", h=120)
    check("too-short tile rejected", False)
except ValueError:
    check("too-short tile rejected", True)
value = [v for v in tiles if v["name"].endswith("_value")][0]
lp = value["visual"]["objects"]["labels"][0]["properties"]
check("tile value shows real number (no auto units)",
      lp["labelDisplayUnits"]["expr"]["Literal"]["Value"] == "1D")
check("tile value font is installed (Segoe UI Semibold)",
      lp["fontFamily"]["expr"]["Literal"]["Value"] == "'Segoe UI Semibold'")
bar = [v for v in vis if v["visual"]["visualType"] == "clusteredBarChart"][0]
dp = bar["visual"]["objects"]["dataPoint"][0]
check("bar fill driven by highlight measure",
      dp["properties"]["fill"]["solid"]["color"]["expr"]["Measure"]["Property"] == hl)
check("bar fill uses instance wildcard selector",
      dp["selector"]["data"][0]["dataViewWildcard"]["matchingOption"] == 1)

print("\n== 5. Background: one slot per tile, icon tile drawn ==")
from PIL import Image
bg = glob.glob(os.path.join(paths["report"], "StaticResources", "RegisteredResources",
                            "bg_*.png"))
check("background generated", len(bg) == 1)
img = Image.open(bg[0]).convert("RGB")
tile0 = kvis[0]
# tile 0 icon square sits at (x+14, y+14) size 36 in the tile color
px = img.getpixel((tile0["x"] + 14 + 4, tile0["y"] + 14 + 18))
want = tuple(int(STYLES["dark_analytics"]["tile_colors"][0][i:i + 2], 16) for i in (1, 3, 5))
check("icon tile painted in tile color", all(abs(a - b) < 40 for a, b in zip(px, want)),
      f"{px} vs {want}")
# between the value card and the note card there must be NO slot seam:
# the pixel just below the value card is the card color, not the page color
card = tuple(int(STYLES["dark_analytics"]["card_bg"][i:i + 2], 16) for i in (1, 3, 5))
mid = img.getpixel((tile0["x"] + tile0["w"] // 2, tile0["y"] + 100))
check("tile parts share one card slot", all(abs(a - b) < 12 for a, b in zip(mid, card)),
      f"{mid} vs {card}")

print("\n== 5b. app_shell: labeled menu + header controls ==")
from layout_templates import RAIL_W, app_shell
from design_measures import add_data_through
dt = add_data_through(m, "sales", "order_date", force=True)
check("data-through measure written", dt in m.list_measures("sales"))
check("data-through ignores filters", "REMOVEFILTERS('sales')" in
      "\n".join(m._table_file("sales").lines))
shell = app_shell(
    style="dark_analytics", table="sales", page="new:Shell", page_index=1,
    pages=[("Page 1", "home"), ("Shell", "chart")],
    brand={"name": "Sales Hub", "tagline": "test", "glyph": "cart"},
    header={"title": "Shell page", "subtitle": "test"},
    kpis=[{"title": "Revenue", "measure": "Total revenue", "icon": "money",
           "trend_col": "order_month"}],
    hero={"type": "bar_chart", "title": "Regions", "category_col": "region",
          "measure": "Total revenue"},
    controls=[{"column": "region", "slicer_type": "tiles", "show_header": False,
               "w": 300, "h": 40}],
    footer_measure=dt, footer_note="Source: test")
sv = shell["pages"][0]["visuals"]
check("content starts right of the menu",
      min(v["x"] for v in sv if v["type"] in ("kpi_tile", "bar_chart")) >= RAIL_W + 20)
ctrl = [v for v in sv if v.get("slot") is False]
check("header controls are slot-free", {v["type"] for v in ctrl} == {"slicer", "clear_button"})
check("header controls inside the band", all(v["y"] + v["h"] <= 90 for v in ctrl))
bg = shell["pages"][0]["background"]
check("background reserves header space for controls", bg["reserve_right"] >= 300 + 110)
check("menu items: one per page, current lit",
      [i["active"] for i in bg["nav_rail"]["items"]] == [False, True])
ed = PbirEditor(paths["report"], guard_pbi=False)   # re-read the model: new measure
ed.build_from_spec(shell)
pg = ed.get_page("Shell")
names = {v["visual"]["visualType"]: v for v in pg._visuals}
nav = names["pageNavigator"]["visual"]["objects"]
check("menu is vertical (layout.orientation 1)",
      nav["layout"][0]["properties"]["orientation"]["expr"]["Literal"]["Value"] == "1D")
sel = [o for o in nav["fill"] if o.get("selector", {}).get("id") == "selected"]
check("selected page tinted with the accent", len(sel) == 1)
check("menu text indented for icons",
      nav["text"][0]["properties"]["leftMargin"]["expr"]["Literal"]["Value"] == "40L")
btn = names["actionButton"]
check("clear button fires ClearAllSlicers",
      btn["visual"]["visualContainerObjects"]["visualLink"][0]["properties"]["type"]
      ["expr"]["Literal"]["Value"] == "'ClearAllSlicers'")
sl = names["slicer"]["visual"]["objects"]
check("tile slicer horizontal", sl["general"][0]["properties"]["orientation"]
      ["expr"]["Literal"]["Value"] == "1D")
check("header controls named ns_ (no slot, no shift)",
      btn["name"].startswith("ns_") and names["slicer"]["name"].startswith("ns_"))
check("header controls not shifted below the band", names["slicer"]["position"]["y"] < 90)
shell_bg = [f for f in glob.glob(os.path.join(paths["report"], "StaticResources",
            "RegisteredResources", "bg_*.png")) if pg.page_id in f]
img2 = Image.open(shell_bg[0]).convert("RGB")
rail_px = img2.getpixel((RAIL_W // 2, 600))
check("menu rail painted in card color",
      all(abs(a - b) < 12 for a, b in zip(rail_px, card)), f"{rail_px} vs {card}")
logo_px = img2.getpixel((18 + 4, 22 + 18))
acc = tuple(int(STYLES["dark_analytics"]["accent"][i:i + 2], 16) for i in (1, 3, 5))
check("brand logo tile in accent", all(abs(a - b) < 40 for a, b in zip(logo_px, acc)),
      f"{logo_px} vs {acc}")

print("\n== 5c. new chart types ==")
from design_measures import add_top_insight
ins = add_top_insight(m, "sales", "Total revenue", "region", noun="revenue", force=True)
body4 = "\n".join(m._table_file("sales").lines)
check("top insight uses TOPN over ALLSELECTED", "TOPN(1, ALLSELECTED('sales'[region])" in body4)
try:
    add_top_insight(m, "sales", "Total revenue", "region", template="{nope}", force=True)
    check("bad insight placeholder rejected", False)
except ValueError:
    check("bad insight placeholder rejected", True)
ed = PbirEditor(paths["report"], guard_pbi=False)
pc = ed.add_page("Charts")
pc.add_table("Scorecard", "sales", columns=["region"],
             measures=["Total revenue", "Orders placed"],
             sparklines=[{"measure": ctx["trend"], "date_col": "order_month",
                          "name": "Trend", "after": "Total revenue"}],
             trend_icons={"Total revenue": ctx["change"]}, x=20, y=20, w=600, h=300)
tbl = pc._visuals[-1]["visual"]
projs = tbl["query"]["queryState"]["Values"]["projections"]
check("sparkline placed after its column",
      [p["nativeQueryRef"] for p in projs] == ["region", "Total revenue", "Trend", "Orders placed"])
check("sparkline uses SparklineData grouping by month",
      projs[2]["field"]["SparklineData"]["Groupings"][0]["Column"]["Property"] == "order_month")
icon = tbl["objects"]["values"][0]
check("trend icons keyed to the column", icon["selector"]["metadata"] == "sales.Total revenue")
cases = icon["properties"]["icon"]["value"]["expr"]["Conditional"]["Cases"]
check("icons: up / down / flat by the change sign",
      [c["Value"]["Literal"]["Value"] for c in cases] ==
      ["'ColoredArrowUp'", "'ColoredArrowDown'", "'ColoredArrowRight'"])
try:
    pc.add_table("x", "sales", columns=["region"], measures=["Total revenue"],
                 trend_icons={"Orders placed": ctx["change"]})
    check("icons on a measure not in the table rejected", False)
except ValueError:
    check("icons on a measure not in the table rejected", True)
pc.add_decomposition_tree("Drill", "sales", "Total revenue",
                          explain_by=["order_id", "region"], expand=["region"],
                          x=20, y=340, w=600, h=360)
tree = pc._visuals[-1]["visual"]
ex = tree["query"]["queryState"]["ExplainBy"]["projections"]
check("expanded column leads the explain-by list",
      [(p["nativeQueryRef"], p["active"]) for p in ex] == [("region", True), ("order_id", False)])
lv = tree["expansionStates"][0]["levels"]
check("every explain-by column has a level; only expanded ones pinned",
      [(l["queryRefs"][0], l.get("isPinned", False)) for l in lv] ==
      [("sales.region", True), ("sales.order_id", False)])
pc.add_donut_chart("Share", "sales", "region", measure="Total revenue",
                   center_measure="Total revenue", center_label="revenue",
                   x=640, y=20, w=400, h=300)
center = pc._visuals[-1]
check("donut center card floats (ns_) inside the donut",
      center["name"].startswith("ns_") and 640 < center["position"]["x"] < 1040)
pc.add_insight_panel("Key insights", "sales", [ins], x=640, y=340, w=400, h=200)
check("insight panel: title + one card per insight share a slot",
      sum(1 for v in pc._visuals if v["name"].startswith("kt_")) == 2 and len(pc._tiles) == 1)
pc.save()

print("\n== 5d. brand styles ==")
from brand_styles import derive_style, contrast_report, build_theme
for brand, mode, hdr in [("#6366F1", "dark", "band"), ("#1D4ED8", "light", "filled"),
                         ("#E4572E", "dark", "band"), ("#FFD400", "light", "filled"),
                         ("#0B3D2E", "dark", "band"), ("#9CA3AF", "light", "band")]:
    st = derive_style(brand, mode, hdr)
    cr = contrast_report(st)
    ok = (cr["accent_vs_card"] >= 3.0 and cr["text_secondary_vs_card"] >= 4.5
          and cr["min_tile_vs_white"] >= 3.0 and cr.get("white_on_header", 9) >= 4.5)
    check(f"brand {brand} {mode}/{hdr}: contrast targets met", ok, str(cr))
    need = set(STYLES["dark_analytics"]) - {"header_fill"}
    check(f"brand {brand}: every style key present", need <= set(st), str(need - set(st)))
th = build_theme("t", derive_style("#6366F1", "dark"))
txt = json.dumps(th)
check("theme swaps every base UI color", not any(c in txt.upper() for c in
      ("#1A2033", "#242B3D", "#3A4260", "#A0A8BC")))
check("theme data colors = style data colors",
      th["dataColors"] == derive_style("#6366F1", "dark")["data_colors"])
for bad in [("#12345", "dark", "band"), ("#123456", "grey", "band"), ("#123456", "dark", "hat")]:
    try:
        derive_style(*bad)
        check(f"bad input {bad} rejected", False)
    except ValueError:
        check(f"bad input {bad} rejected", True)
for name in ("midnight_indigo", "executive_blue"):
    check(f"preset '{name}' registered in STYLES", name in STYLES)
    check(f"preset '{name}' theme file exists", os.path.exists(
        os.path.join(THEMES_DIR, f"professional_{name}.json")))

print("\n== 6. Schema validation ==")
r = subprocess.run([sys.executable, os.path.join(os.path.dirname(HERE), "_validate_pbir.py"), paths["report"]],
                   capture_output=True, text=True, encoding="utf-8")
tail = (r.stdout or "").strip().splitlines()[-1:] or ["(no output)"]
check("PBIR schema: ALL VALID", r.returncode == 0 and "ALL VALID" in tail[0],
      (r.stdout or "")[-1500:] + (r.stderr or "")[-800:])
r = subprocess.run([sys.executable, os.path.join(os.path.dirname(HERE), "_check_model_names.py"), TMP],
                   capture_output=True, text=True, encoding="utf-8")
check("model names OK", r.returncode == 0, (r.stdout or "")[-800:])

print("\n" + "=" * 60)
print(f"PASS: {len(PASS)}   FAIL: {len(FAIL)}")
if FAIL:
    print("FAILED:\n  " + "\n  ".join(FAIL))
    sys.exit(1)
print("ALL TESTS PASSED")
