# Headless tests for the SVG-measure path: dual-mode scalars, the escaping
# rules, every primitive through the offline render harness, the TMDL and PBIR
# wiring, and the gate's ability to FAIL — a gate that only ever passes is not
# a gate.
#
#     python tests/_test_svg.py
#
# No Power BI, no network, no fixture. Builds a throwaway project in %TEMP%.
import json
import glob
import os
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)          # the gate scripts live at the repo root
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import pbip_scaffold                                                # noqa: E402
import svg_measures as S                                            # noqa: E402
from brand_styles import (SVG_TOKENS, derive_theme_pair, svg_tokens,  # noqa: E402
                          token_contrast)
from _offline import SCHEMA_SKIP_REASON, schemas_available          # noqa: E402
from pbir_editor import STYLES, PbirEditor                          # noqa: E402
from tmdl_editor import TmdlModel                                    # noqa: E402

PASS, FAIL, SKIP = [], [], []


def check(label, cond, detail=""):
    (PASS if cond else FAIL).append(label)
    print(("  ok   " if cond else "  FAIL ") + label
          + (" — %s" % detail if detail and not cond else ""))


def skip(label, reason):
    SKIP.append("%s — %s" % (label, reason))
    print("  SKIP %s — %s" % (label, reason))


def raises(label, fn, exc=Exception):
    try:
        fn()
    except exc:
        check(label, True)
        return
    check(label, False, "no exception raised")


print("== 1. Dual-mode scalars: one pass gives DAX and the sample ==")
a = S.measure("Hires", 120)
b = S.measure("Target", 100)
r = (a / b) * 50
check("arithmetic keeps the sample", abs(r.val - 60) < 1e-9, r.val)
check("division emits DIVIDE, not /", "DIVIDE" in r.dax and "/" not in r.dax, r.dax)
check("DIVIDE by zero is 0, not an error", (a / S.num(0)).val == 0)
check("clamp holds both ends", (a / b).clamp(0, 1).val == 1.0)
check("FORMAT carries the locale", '"en-US"' in a.fmt("#,0").dax)
check("sample formatting matches the pattern", a.fmt("#,0").val == "120")
for val, pat, want in [(1284.4, "#,0", "1,284"), (0.0823, "0.0%", "8.2%"),
                       (2.5, "0", "3"), (-2.5, "0", "-3"),
                       (1234567.891, "#,0.00", "1,234,567.89")]:
    check("py_format %r %s -> %s" % (val, pat, want),
          S.py_format(val, pat) == want, S.py_format(val, pat))
raises("an unsupported FORMAT pattern raises", lambda: S.py_format(1, "0.000"))

print("\n== 2. Escaping ==")
d = S.SvgDoc(100, 50, S.TOKENS_LIGHT, name="esc")
d.rect(0, 0, 100, 50, d.color("card"))
dax = d.build().dax
pct, hsh = dax.find('"%", "%25"'), dax.find('"#", "%23"')
check("the measure escapes % and #", pct >= 0 and hsh >= 0)
check("% is escaped BEFORE # (reversed gives %2523)", pct < hsh,
      "percent at %d, hash at %d" % (pct, hsh))
check("unescape reverses in the opposite order",
      S.unescape_uri("%25 %23") == "% #")
dirty = S.xml_text(S.text_measure("Client", "A & B <Ltd>"))
check("data text is xml-escaped at refresh time",
      dirty.val == "A &amp; B &lt;Ltd&gt;", dirty.val)
check("the ampersand is substituted first",
      dirty.dax.index('"&", "&amp;"') < dirty.dax.index('"<", "&lt;"'))
check("a literal is escaped in Python, not with SUBSTITUTE",
      "SUBSTITUTE" not in S.xml_text(S.lit("A & B")).dax)
check("attribute escaping covers the quote",
      S.xml_attr(S.text_measure("x", "O'Neil")).val == "O&apos;Neil")

print("\n== 3. Text fitting ==")
small = S.fit_font_size(S.lit("1,284"), 180)
big = S.fit_font_size(S.lit("174,078,312"), 180)
check("a short value takes the largest size", small.val == "34", small.val)
check("a long value steps down", int(big.val) < 34, big.val)
check("the ladder is decided at refresh, not compose time",
      "SWITCH" in big.dax and "LEN" in big.dax)
trunc = S.fit_truncate(S.text_measure("c", "A very long client name indeed"), 90, 12)
check("a long label is truncated", trunc.val.endswith("…"), trunc.val)
check("truncation still escapes", "SUBSTITUTE" in trunc.dax)
check("a short label is left alone",
      S.fit_truncate(S.text_measure("c", "Acme"), 90, 12).val == "Acme")

print("\n== 4. Series: the stagger must come from [Value] ==")
spec = S.SeriesSpec({"@v": "[Hires]", "@lab": "[Month]"},
                    [{"@v": v, "@lab": m} for v, m in
                     zip([3, 9, 5], ["Jan", "Feb", "Mar"])])
card = S.kpi_sparkbar(348, 119, "T", value=("Hires", 17), series=spec,
                      tokens=S.TOKENS_LIGHT, name="series")
check("bars animate on a computed begin, not a constant",
      "begin='\" & FORMAT ( ( ( [Value] - 1 )" in card.dax
      or "[Value] - 1 ) * 0.07" in card.dax, "stagger is constant")
check("the series is one CONCATENATEX over ADDCOLUMNS/GENERATESERIES",
      "CONCATENATEX" in card.dax and "GENERATESERIES ( 1, 3 )" in card.dax)
check("the sample render draws every row", card.svg.count("<rect") >= 4)
raises("a sample row missing a column is rejected",
       lambda: S.SvgDoc(10, 10, S.TOKENS_LIGHT).series({"@v": "1", "@z": "2"},
                                                       [{"@v": 1}]))

print("\n== 5. Every primitive renders, and is sized to its card ==")
clients = S.SeriesSpec({"@v": "[Hires]", "@lab": "[Client]"},
                       [{"@v": v, "@lab": n} for v, n in
                        [(128, "Northwind & Co"), (96, "Acme"), (74, "Globex")]])
cases = {
    "kpi_sparkbar": lambda t: S.kpi_sparkbar(
        348, 119, "Hires", value=("Hires", 1284), change=("Chg", "+8.2%"),
        icon="trend", series=spec, tokens=t),
    "gauge_arc": lambda t: S.gauge_arc(
        348, 240, value=("Rate", 0.62), title="Acceptance", caption="of 148",
        value_format="0%", min_label="0%", max_label="100%", tokens=t),
    "bar_list": lambda t: S.bar_list(348, 265, clients, title="By client", tokens=t),
    "donut": lambda t: S.donut(444, 265, clients, title="Mix",
                               center_value=("N", 298), tokens=t),
    "line_area": lambda t: S.line_area(444, 295, spec, title="Weekly", tokens=t),
    "progress_track": lambda t: S.progress_track(
        348, 110, "Accepted", value=("Rate", 0.62), target=("T", 0.75),
        value_format="0%", caption="target 75%", tokens=t),
}
check("every primitive in the brief is implemented",
      set(cases) == set(S.PRIMITIVES), sorted(set(cases) ^ set(S.PRIMITIVES)))
for mode, tokens in (("light", S.TOKENS_LIGHT), ("dark", S.TOKENS_DARK)):
    for label, fn in cases.items():
        card = fn(tokens)
        try:
            S.render_sample(card)
            ok, why = True, ""
        except Exception as ex:
            ok, why = False, str(ex)
        check("%s/%s renders and is well-formed" % (label, mode), ok, why)
        root = ET.fromstring(card.svg)
        check("%s/%s svg size == card size" % (label, mode),
              float(root.get("width")) == card.width
              and float(root.get("height")) == card.height)
        check("%s/%s stretches rather than letterboxes" % (label, mode),
              root.get("preserveAspectRatio") == "none")
        check("%s/%s unpacks as (dax, width, height)" % (label, mode),
              len(tuple(card)) == 3)
        check("%s/%s reads its colours from measures" % (label, mode),
              "[Color " in card.dax)
        check("%s/%s skeleton parses like the gate reads it" % (label, mode),
              ET.fromstring(S.live_svg(card.dax)) is not None)
        baked = [k for k in ("ink", "card", "accent", "muted", "track2")
                 if tokens[k] in card.dax]
        check("%s/%s bakes no theme colour into the DAX" % (label, mode),
              not baked, "hard-coded %s — the slicer cannot reach it" % baked)

print("\n== 6. Colour tokens ==")
pair = derive_theme_pair("#C8963C")
check("a light/dark pair carries every token",
      set(pair["light"]) == set(SVG_TOKENS) == set(pair["dark"]))
check("20 tokens", len(SVG_TOKENS) == 20, len(SVG_TOKENS))
check("the two modes actually differ",
      sum(pair["light"][k] != pair["dark"][k] for k in SVG_TOKENS) >= 15)
for mode in ("light", "dark"):
    c = token_contrast(pair[mode])
    check("%s: body text reaches 4.5:1 on the card" % mode, c["ink_vs_card"] >= 4.5, c)
    check("%s: secondary text reaches 4.5:1" % mode, c["muted_vs_card"] >= 4.5, c)
    check("%s: the accent reaches 3:1 (WCAG non-text)" % mode,
          c["accent_vs_card"] >= 3.0, c)
    check("%s: the glyph survives on its chip" % mode,
          c["chipicon_vs_chip"] >= 2.9, c)
    check("%s: a de-emphasised bar is still visible" % mode,
          c["accentsoft_vs_card"] >= 1.35, c)
for name in ("corporate_light", "dark_analytics", "minimal_neutral"):
    t = svg_tokens(STYLES[name])
    check("the built-in preset '%s' yields tokens" % name, set(t) == set(SVG_TOKENS))
raises("an incomplete palette is rejected",
       lambda: S.kpi_sparkbar(100, 50, "x", 1, tokens={"ink": "#000"}))
raises("an unknown colour token is rejected",
       lambda: S.SvgDoc(10, 10, {"ink": "#000"}).color("nope"), KeyError)

print("\n== 7. Skeleton extraction (what the gate reads) ==")
sk = S.skeleton('"<a x=\'" & FORMAT ( [m], "0.0", "en-US" ) & "\'/>"')
check("function arguments are not document text", sk == "<a x='0'/>", sk)
check("nested VAR/RETURN do not break the split",
      [n for n, _ in S.split_vars(
          'VAR _a = CONCATENATEX ( t, VAR _x = 1 RETURN "y", "" )\n'
          'VAR _b = 2\nRETURN _b')] == ["_a", "_b", "RETURN"])
check("a mark body is found inside CONCATENATEX",
      S.concat_bodies('CONCATENATEX ( _r, "<rect/>", "", [Value], ASC )')
      == [' "<rect/>"'])
docs = S.svg_documents('RETURN IF ( x, "<svg width=\'1\' height=\'2\'></svg>", '
                       '"<svg width=\'3\' height=\'4\'></svg>" )')
check("both branches of a conditional document are found", len(docs) == 2, docs)
check("a data-URI prefix sharing the literal is trimmed",
      S.svg_documents('RETURN "data:image/svg+xml;utf8,<svg a=\'1\'></svg>"')[0]
      .startswith("<svg"))

print("\n== 8. End-to-end: build a project and run all three gates ==")
TMP = os.path.join(tempfile.gettempdir(), "pbip_svg_suite")
shutil.rmtree(TMP, ignore_errors=True)
os.makedirs(TMP)
csv = os.path.join(TMP, "feed.csv")
with open(csv, "w", encoding="utf-8") as f:
    f.write("id,client,hires\n")
    for i in range(30):
        f.write("%d,%s,%d\n" % (i, ["Acme", "Globex", "Initech"][i % 3], i % 5))
paths = pbip_scaffold.new_project(TMP, "svgtest", csv, table_name="feed")
model = TmdlModel(paths["semantic_model"], guard_pbi=False)
model.add_calculated_table("_Measures", 'ROW ( "x", 1 )',
                           [{"name": "x", "type": "int64", "hidden": True}],
                           force=True)
model.add_measure("_Measures", "Hires", "SUM ( feed[hires] )",
                  format_string="#,0", force=True)
names = S.add_theme_measures(model, pair["light"], pair["dark"],
                             table="_Measures", force=True)
check("the theme writer emits Selected theme + one measure per token",
      len(names) == 21 and names[0] == "Selected theme", len(names))
model_txt = open(os.path.join(paths["semantic_model"], "definition", "model.tmdl"),
                 encoding="utf-8-sig").read()
check("a calculated table is referenced from model.tmdl",
      "ref table Theme" in model_txt and "ref table _Measures" in model_txt)
theme_txt = open(os.path.join(paths["semantic_model"], "definition", "tables",
                              "Theme.tmdl"), encoding="utf-8-sig").read()
check("the Theme table is a two-row disconnected DATATABLE",
      "DATATABLE" in theme_txt and '"Light", 1' in theme_txt
      and '"Dark", 2' in theme_txt)
check("its partition is calculated, not M",
      "partition Theme = calculated" in theme_txt)
check("calculated columns use the bracketed sourceColumn spelling",
      "sourceColumn: [Theme]" in theme_txt)

card = S.add_svg_measure(model, "_Measures", "Clients (SVG)",
                         S.bar_list(348, 265, clients, title="By client",
                                    tokens=pair["light"]), force=True)
meas_txt = open(os.path.join(paths["semantic_model"], "definition", "tables",
                             "_Measures.tmdl"), encoding="utf-8-sig").read()
check("the measure carries dataCategory: ImageUrl",
      "dataCategory: ImageUrl" in meas_txt)
check("dataCategory comes last in the block",
      meas_txt.index("dataCategory: ImageUrl")
      > meas_txt.index("measure 'Clients (SVG)'"))
check("a plain measure gets no dataCategory",
      meas_txt.count("dataCategory:") == 1)

ed = PbirEditor(paths["report"], guard_pbi=False)
ed.build_from_spec({"pages": [{"page": 0, "clear": True, "visuals": [
    {"type": "slicer", "table": "Theme", "column": "Theme",
     "x": 900, "y": 20, "w": 180, "h": 46},
    {"type": "svg_card", "table": "_Measures", "measure": "Clients (SVG)",
     "x": 40, "y": 100, "w": card.width, "h": card.height},
]}]}, force=True)
vis = [json.load(open(p, encoding="utf-8")) for p in
       glob.glob(os.path.join(paths["report"], "definition", "pages", "*",
                              "visuals", "*", "visual.json"))]
svg_vis = [v for v in vis if v["visual"]["visualType"] == "cardVisual"]
check("the spec builds a cardVisual", len(svg_vis) == 1, [v["visual"]["visualType"] for v in vis])
v = svg_vis[0]
objs = v["visual"]["objects"]
img = objs["cardImage"][0]["properties"]
check("the image is bound to the measure, not a URL",
      img["imageData"]["expr"]["Measure"]["Property"] == "Clients (SVG)")
check("imageType is imageData", img["imageType"]["expr"]["Literal"]["Value"]
      == "'imageData'")
check("imageDataFit is Fit", img["imageDataFit"]["expr"]["Literal"]["Value"] == "'Fit'")
check("imageHeight matches the card", img["imageHeight"]["expr"]["Literal"]["Value"]
      == "%dL" % round(card.height))
check("the callout is collapsed to zero",
      any(o["properties"].get("calloutSize", {}).get("expr", {})
          .get("Literal", {}).get("Value") == "0D" for o in objs["layout"]))
check("the value layer is off",
      objs["value"][0]["properties"]["show"]["expr"]["Literal"]["Value"] == "false")
check("the label layer is off",
      objs["label"][0]["properties"]["show"]["expr"]["Literal"]["Value"] == "false")
vco = v["visual"]["visualContainerObjects"]
for key in ("visualHeader", "title", "background", "border", "dropShadow",
            "visualTooltip"):
    check("container %s is off" % key,
          vco[key][0]["properties"]["show"]["expr"]["Literal"]["Value"] == "false")
check("the card floats (no slot drawn behind a card that paints its own)",
      v["name"].startswith("ns_"), v["name"])
check("the position matches the SVG",
      v["position"]["width"] == card.width and v["position"]["height"] == card.height)


def gate(script, target, want):
    r = subprocess.run([sys.executable, os.path.join(ROOT, script), target],
                       capture_output=True, text=True, encoding="utf-8")
    return want in (r.stdout or ""), r.returncode, (r.stdout or "")[-1200:]


HAVE_SCHEMAS = schemas_available()
for script, target, want in [("_check_model_names.py", TMP, "NAMES OK"),
                             ("_validate_pbir.py", paths["report"], "ALL VALID"),
                             ("_validate_svg_measures.py", TMP, "SVG MEASURES OK")]:
    # the PBIR gate fetches Microsoft's schemas; unreachable and uncached, it
    # cannot run at all, and a skip beats a false failure
    if script == "_validate_pbir.py" and not HAVE_SCHEMAS:
        skip("gate %s prints %s" % (script, want), SCHEMA_SKIP_REASON)
        continue
    ok, rc, out = gate(script, target, want)
    check("gate %s prints %s" % (script, want), ok, out)
    if script == "_validate_svg_measures.py":
        check("the SVG gate exits 0 on a clean project", rc == 0, rc)

print("\n== 9. The gate must FAIL when it should ==")
MUT = os.path.join(tempfile.gettempdir(), "pbip_svg_mutate")
MEAS_TMDL = os.path.join("svgtest.SemanticModel", "definition", "tables",
                         "_Measures.tmdl")


def mutate(fn_measures=None, fn_visual=None):
    shutil.rmtree(MUT, ignore_errors=True)
    shutil.copytree(TMP, MUT)
    if fn_measures:
        p = os.path.join(MUT, MEAS_TMDL)
        t = open(p, encoding="utf-8-sig").read()
        open(p, "w", encoding="utf-8", newline="").write(fn_measures(t))
    if fn_visual:
        for p in glob.glob(os.path.join(MUT, "svgtest.Report", "definition",
                                        "pages", "*", "visuals", "*",
                                        "visual.json")):
            doc = json.load(open(p, encoding="utf-8"))
            if doc.get("visual", {}).get("objects", {}).get("cardImage"):
                json.dump(fn_visual(doc), open(p, "w", encoding="utf-8"), indent=2)
                break
    r = subprocess.run([sys.executable,
                        os.path.join(ROOT, "_validate_svg_measures.py"), MUT],
                       capture_output=True, text=True, encoding="utf-8")
    return r.returncode, (r.stdout or "")


def resize(doc):
    doc["position"]["width"] = doc["position"]["width"] + 9
    return doc


for label, kwargs, expect in [
    ("a malformed document",
     {"fn_measures": lambda t: t.replace('"\'/>"', '"\'>"', 1)},
     "does not parse"),
    ("a document with no closing tag at all",
     {"fn_measures": lambda t: t.replace("</svg>", "", 1)},
     "no complete <svg> document"),
    ("a malformed mark body", {"fn_measures": lambda t: t.replace("</text>", "<text>", 1)},
     "mark body does not parse"),
    ("the escaping order reversed",
     {"fn_measures": lambda t: t.replace(
         'SUBSTITUTE ( SUBSTITUTE ( _svg, "%", "%25" ), "#", "%23" )',
         'SUBSTITUTE ( SUBSTITUTE ( _svg, "#", "%23" ), "%", "%25" )')},
     "escaped before"),
    ("a card resized away from its SVG", {"fn_visual": resize}, "stretched"),
    ("dataCategory removed",
     {"fn_measures": lambda t: t.replace("\t\tdataCategory: ImageUrl", "", 1)},
     "does not declare dataCategory"),
]:
    rc, out = mutate(**kwargs)
    check("the gate rejects %s" % label, rc == 1 and expect in out,
          out[-700:])
shutil.rmtree(MUT, ignore_errors=True)

rc, out = mutate()
check("the gate still passes the untouched project", rc == 0, out[-400:])
shutil.rmtree(MUT, ignore_errors=True)

print("\n" + "=" * 62)
print("PASS: %d   FAIL: %d   SKIP: %d" % (len(PASS), len(FAIL), len(SKIP)))
if FAIL:
    print("FAILED:\n  " + "\n  ".join(FAIL))
    sys.exit(1)
print("ALL SVG TESTS PASSED")
