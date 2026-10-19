# Build a complete .pbip project whose cards are SVG measures, end to end,
# from this folder alone and with Power BI Desktop closed.
#
#     python tests/build_svg_smoke.py [out_dir]
#
# Writes (default <repo>/out/svg_smoke, override with PBIMAKE_SMOKE_OUT):
#   svg_smoke.pbip / .Report / .SemanticModel   the project
#   samples/*.svg                               every primitive, rendered
#   samples/sheet.html                          all of them on one page
#
# Then run the three gates, all of which must pass:
#   python _check_model_names.py      <out>
#   python _validate_pbir.py          <out>/svg_smoke.Report
#   python _validate_svg_measures.py  <out>
import os
import random
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import pbip_scaffold                                            # noqa: E402
import svg_measures as S                                        # noqa: E402
from brand_styles import derive_theme_pair                      # noqa: E402
from pbir_editor import PbirEditor                              # noqa: E402
from tmdl_editor import TmdlModel                               # noqa: E402

BRAND = "#C8963C"
OUT = (sys.argv[1] if len(sys.argv) > 1
       else os.environ.get("PBIMAKE_SMOKE_OUT", os.path.join(ROOT, "out", "svg_smoke")))
MEAS = "_Measures"
CANVAS = (1280, 760)

STAGES = ["CV sent", "Screening", "Interview", "Final", "Offer"]
CLIENTS = ["Northwind Logistics & Co", "Acme Robotics", "Globex", "Initech",
           "Umbrella Health"]
MONTHS = ["Apr", "May", "Jun", "Jul", "Aug", "Sep"]


# ── 1. data ───────────────────────────────────────────────────────────────────

def write_csv(path):
    """A small hiring feed. Generated, never committed — see .gitignore."""
    rng = random.Random(7)
    rows = ["candidate_id,client,stage,month,week,cv_count,hire_count,offer_value"]
    for i in range(400):
        rows.append("c%03d,%s,%s,%s,W%02d,%d,%d,%d" % (
            i, CLIENTS[i % len(CLIENTS)], STAGES[i % len(STAGES)],
            MONTHS[i % len(MONTHS)], 1 + i % 12,
            rng.randint(1, 9), rng.randint(0, 2), rng.randint(2000, 18000)))
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write("\n".join(rows) + "\n")
    return path


# ── 2. the cards ──────────────────────────────────────────────────────────────
# Each entry is one SVG measure and the card that hosts it. The sample values
# are what the offline render draws; the DAX is what Power BI draws.

def cards(tokens):
    spark = S.SeriesSpec(
        {"@v": "CALCULATE ( [Hires], ALL ( feed ), feed[month] = "
               "SELECTEDVALUE ( 'Month order'[month] ) )",
         "@lab": 'FORMAT ( [Value], "0" )'},
        [{"@v": v, "@lab": m} for v, m in zip([34, 41, 38, 52, 47, 61], MONTHS)])
    by_client = S.SeriesSpec(
        {"@v": "CALCULATE ( [Hires] )", "@lab": "SELECTEDVALUE ( feed[client] )"},
        [{"@v": v, "@lab": n} for v, n in
         zip([128, 96, 74, 52, 31], CLIENTS)])
    by_stage = S.SeriesSpec(
        {"@v": "CALCULATE ( [Candidates] )", "@lab": "SELECTEDVALUE ( feed[stage] )"},
        [{"@v": v, "@lab": n} for v, n in
         zip([446, 268, 212, 98, 44], STAGES)])
    weekly = S.SeriesSpec(
        {"@v": "CALCULATE ( [CVs] )", "@lab": "SELECTEDVALUE ( feed[week] )"},
        [{"@v": v, "@lab": "W%d" % (i + 1)} for i, v in
         enumerate([120, 142, 131, 168, 155, 190, 176, 205, 188, 221, 240, 232])])

    k = dict(tokens=tokens)
    return [
        ("KPI hires (SVG)", 40, 108,
         S.kpi_sparkbar(348, 119, "Hires closed", value=("Hires", 1284),
                        change=("Hires change label", "+8.2%"),
                        change_color=("Hires change color", "#15803D"),
                        icon="trend", series=spark, name="kpi_sparkbar", **k)),
        ("KPI accept (SVG)", 408, 108,
         S.progress_track(348, 119, "Offers accepted",
                          value=("Accept rate", 0.62),
                          target=("Accept target", 0.75), value_format="0%",
                          caption="target 75%", name="progress_track", **k)),
        ("KPI pipeline (SVG)", 776, 108,
         S.kpi_sparkbar(348, 119, "CVs sent", value=("CVs", 24680),
                        change=("CVs change label", "-3.1%"),
                        change_color="negative", icon="stack", series=weekly,
                        name="kpi_cvs", **k)),
        ("Clients (SVG)", 40, 251,
         S.bar_list(348, 265, by_client, title="Hires by client",
                    name="bar_list", **k)),
        ("Pipeline (SVG)", 408, 251,
         S.donut(348, 265, by_stage, title="Pipeline by stage",
                 center_value=("Candidates", 1068), center_caption="candidates",
                 legend=False, name="donut", **k)),
        ("Acceptance (SVG)", 776, 251,
         S.gauge_arc(348, 265, value=("Accept rate", 0.62),
                     title="Offer acceptance", caption="of 148 offers",
                     value_format="0%", min_label="0%", max_label="100%",
                     name="gauge_arc", **k)),
        ("Weekly CVs (SVG)", 40, 540,
         S.line_area(716, 180, weekly, title="CVs per week",
                     caption="rolling 12 weeks", name="line_area", **k)),
        ("Stage mix (SVG)", 776, 540,
         S.donut(348, 180, by_stage, center_value=("Candidates", 1068),
                 center_caption="total", legend=True, hole=0.66,
                 name="donut_legend", **k)),
    ]


# ── 3. build ──────────────────────────────────────────────────────────────────

def build():
    if os.path.isdir(OUT):
        shutil.rmtree(OUT)
    os.makedirs(OUT)
    csv = write_csv(os.path.join(OUT, "feed.csv"))
    paths = pbip_scaffold.new_project(OUT, "svg_smoke", csv, table_name="feed",
                                      page_name="Overview", canvas=CANVAS)

    model = TmdlModel(paths["semantic_model"], guard_pbi=False)
    # a measures-only table: SVG measures belong nowhere near the feed
    model.add_calculated_table(MEAS, 'ROW ( "x", 1 )',
                               [{"name": "x", "type": "int64", "hidden": True}],
                               force=True)
    for name, dax, fmt in [
        ("Hires", "SUM ( feed[hire_count] )", "#,0"),
        ("CVs", "SUM ( feed[cv_count] )", "#,0"),
        ("Candidates", "COUNTROWS ( feed )", "#,0"),
        ("Accept rate", "DIVIDE ( [Hires], [Candidates] )", "0%"),
        ("Accept target", "0.75", "0%"),
    ]:
        model.add_measure(MEAS, name, dax, format_string=fmt, force=True)
    for name, dax in [
        ("Hires change label", '"+8.2%"'),
        ("Hires change color", '"#15803D"'),
        ("CVs change label", '"-3.1%"'),
    ]:
        model.add_measure(MEAS, name, dax, text_measure=True, force=True)

    pair = derive_theme_pair(BRAND)
    S.add_theme_measures(model, pair["light"], pair["dark"], table=MEAS,
                         force=True)

    built = cards(pair["light"])
    for name, _x, _y, card in built:
        S.add_svg_measure(model, MEAS, name, card, force=True)

    ed = PbirEditor(paths["report"], guard_pbi=False)
    spec = {
        "style": "corporate_light",
        "pages": [{
            "page": 0, "canvas": {"width": CANVAS[0], "height": CANVAS[1]},
            "clear": True,
            "visuals": (
                [{"type": "text_box", "text": "Hiring — SVG measure smoke test",
                  "x": 40, "y": 28, "w": 600, "h": 44, "font_size": 20,
                  "bold": True}]
                + [{"type": "slicer", "table": "Theme", "column": "Theme",
                    "x": 1050, "y": 30, "w": 190, "h": 46, "title": None}]
                + [{"type": "svg_card", "table": MEAS, "measure": name,
                    "x": x, "y": y, "w": card.width, "h": card.height}
                   for name, x, y, card in built]
            ),
        }],
    }
    ed.build_from_spec(spec, force=True)

    samples = os.path.join(OUT, "samples")
    sheet = []
    for mode in ("light", "dark"):
        for _name, _x, _y, card in cards(pair[mode]):
            path = os.path.join(samples, "%s_%s.svg" % (card.name, mode))
            S.render_sample(card, path)
            sheet.append((mode, card.name, card.svg))
    write_sheet(os.path.join(samples, "sheet.html"), sheet)
    return paths, built, samples


def write_sheet(path, entries):
    """All the samples on one page, light over dark — the eyeball review."""
    css = ("body{font:13px 'Segoe UI',sans-serif;margin:0}"
           ".panel{padding:18px 22px}.light{background:#F7F5F1;color:#1F2328}"
           ".dark{background:#17130D;color:#EFEAE1}"
           ".grid{display:flex;flex-wrap:wrap;gap:16px;align-items:flex-start}"
           "figure{margin:0}figcaption{font-size:11px;opacity:.6;margin-bottom:3px}")
    out = ["<!doctype html><meta charset='utf-8'><title>svg_smoke samples</title>"
           "<style>%s</style>" % css]
    for mode in ("light", "dark"):
        out.append("<div class='panel %s'><h3>%s</h3><div class='grid'>" % (mode, mode))
        for m, name, svg in entries:
            if m == mode:
                out.append("<figure><figcaption>%s</figcaption>%s</figure>"
                           % (name, svg))
        out.append("</div></div>")
    with open(path, "w", encoding="utf-8") as f:
        f.write("".join(out))


# ── 4. gates ──────────────────────────────────────────────────────────────────

def gate(script, target, want):
    r = subprocess.run([sys.executable, os.path.join(ROOT, script), target],
                       capture_output=True, text=True, encoding="utf-8")
    tail = (r.stdout or "").strip().splitlines()
    ok = want in (r.stdout or "")
    print("  %-28s rc=%s  %s" % (script, r.returncode,
                                 tail[-1] if tail else "(no output)"))
    if not ok:
        print((r.stdout or "")[-3000:], (r.stderr or "")[-1500:])
    return ok


def main():
    paths, built, samples = build()
    print("\n== gates ==")
    ok = [
        gate("_check_model_names.py", OUT, "NAMES OK"),
        gate("_validate_pbir.py", paths["report"], "ALL VALID"),
        gate("_validate_svg_measures.py", OUT, "SVG MEASURES OK"),
    ]
    print("\nproject : %s" % paths["pbip"])
    print("samples : %s" % samples)
    print("cards   : %d SVG measures on %d card(s)" % (len(built), len(built)))
    if not all(ok):
        print("\nSMOKE FAILED")
        return 1
    print("\nSVG SMOKE OK — all three gates passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
