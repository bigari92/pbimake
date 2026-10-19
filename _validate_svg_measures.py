# Catch the SVG-measure failures that Power BI reports as a BLANK CARD.
#
# A measure that returns an SVG data URI is rendered by a cardVisual as a
# picture. When it is wrong, nothing says so: no error, no warning, no red
# triangle — just an empty card that looks like a layout bug. The four ways to
# get there, all of them checked here:
#
#   1. The SVG is malformed. One unclosed tag, one stray & from a category
#      label, and the image silently fails to decode.
#   2. The escaping ran in the wrong order. The measure must escape % BEFORE #,
#      because escaping # introduces a % of its own — reverse them and every
#      colour arrives as %2523xxxxxx.
#   3. The SVG's declared size does not match the card hosting it. The document
#      is stretched onto the card by preserveAspectRatio='none', so a mismatch
#      is not a crop — it is a silent distortion of every mark.
#   4. The measure is missing dataCategory: ImageUrl, so the card prints the
#      data URI as text instead of drawing it.
#
# Usage — accepts .pbip project folders, a .SemanticModel folder, or a parent
# folder to sweep:
#     python _validate_svg_measures.py <path> [more paths...]
#
# Exit code 1 if any FAIL is found, so it can gate a build.

import glob
import os
import sys
import json
import xml.etree.ElementTree as ET

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from svg_measures import markup_fragments, svg_documents           # noqa: E402
from tmdl_editor import TmdlTableFile                              # noqa: E402

EPS = 0.05          # px tolerance between the SVG and its card
WRAP = "<svg xmlns='http://www.w3.org/2000/svg'>%s</svg>"


def is_svg(dax):
    """ImageUrl also covers plain URLs and base64 raster data URIs — a photo
    measure is not a defect, it is just not this gate's business."""
    return "image/svg+xml" in dax or "<svg" in dax


def image_measures(model_dir):
    """{measure name: (dax, relpath)} for every measure declaring
    dataCategory: ImageUrl."""
    found = {}
    for path in sorted(glob.glob(os.path.join(model_dir, "definition", "tables",
                                              "*.tmdl"))):
        tf = TmdlTableFile(path)
        rel = os.path.relpath(path, model_dir)
        for name, s, e in tf._block_ranges("measure"):
            block = tf.lines[s:e]
            if not any(l.strip() == "dataCategory: ImageUrl" for l in block):
                continue
            body = [l for l in block
                    if not l.strip().startswith(("lineageTag:", "displayFolder:",
                                                 "dataCategory:", "formatString:",
                                                 "///"))]
            dax = "\n".join(body)
            dax = dax.split("=", 1)[1] if "=" in dax.split("\n")[0] else dax
            found[name] = (dax, rel)
    return found


def svg_cards(report_dir):
    """[(measure, page, visual, type, width, height)] for every visual whose
    card image is bound to a measure."""
    out = []
    pages = os.path.join(report_dir, "definition", "pages")
    for vpath in sorted(glob.glob(os.path.join(pages, "*", "visuals", "*",
                                               "visual.json"))):
        try:
            with open(vpath, encoding="utf-8") as f:
                doc = json.load(f)
        except (OSError, ValueError):
            continue
        visual = doc.get("visual", {})
        for entry in visual.get("objects", {}).get("cardImage", []):
            expr = entry.get("properties", {}).get("imageData", {}).get("expr", {})
            prop = expr.get("Measure", {}).get("Property")
            if not prop:
                continue
            pos = doc.get("position", {})
            parts = vpath.replace("\\", "/").split("/")
            out.append((prop, parts[-4], doc.get("name", parts[-2]),
                        visual.get("visualType", "?"),
                        pos.get("width"), pos.get("height")))
    return out


def check_project(model_dir, report_dir):
    """[(severity, measure_or_None, message)] for one project."""
    problems, skipped = [], []
    measures = image_measures(model_dir)
    cards = svg_cards(report_dir) if report_dir else []

    for name, _page, _vis, vtype, _w, _h in cards:
        if name not in measures:
            problems.append((
                "FAIL", name, "card image is bound to '%s', which does not declare "
                "dataCategory: ImageUrl — the card will print the data URI as "
                "text" % name))
        elif vtype != "cardVisual":
            problems.append((
                "WARN", name, "'%s' is hosted by a %s; only cardVisual renders "
                "objects.cardImage" % (name, vtype)))

    if not measures:
        problems.append(("WARN", None, "no ImageUrl measures in %s"
                         % os.path.basename(model_dir)))

    for name in sorted(measures):
        dax, rel = measures[name]
        if not is_svg(dax):
            skipped.append(name)
            continue
        pct, hsh = dax.find('"%", "%25"'), dax.find('"#", "%23"')
        if pct == -1 and hsh == -1:
            problems.append((
                "WARN", name, "%s: no %% / # escaping found — a literal # in a colour "
                "makes the data URI stop at the fragment" % name))
        elif pct == -1 or hsh == -1 or pct < hsh:
            pass          # % escaped first, or only one of them: correct order
        else:
            problems.append((
                "FAIL", name, "%s: # is escaped before %% — the %% introduced by %%23 is "
                "then escaped again into %%2523 and the card goes blank" % name))

        # a measure may pick between documents (one per theme); each is a card
        # that can break on its own
        docs = svg_documents(dax)
        if not docs:
            problems.append((
                "FAIL", name, "%s (%s): no complete <svg> document found — assemble it "
                "in one VAR so it can be validated without a model"
                % (name, rel)))
            continue

        sizes, broken = [], False
        for k, svg in enumerate(docs):
            tag = "" if len(docs) == 1 else " (document %d of %d)" % (k + 1, len(docs))
            try:
                root = ET.fromstring(svg)
            except ET.ParseError as ex:
                problems.append((
                    "FAIL", name, "%s (%s): the SVG%s does not parse — %s\n        %s"
                    % (name, rel, tag, ex, _around(svg, ex))))
                broken = True
                continue
            if root.tag.split("}")[-1] != "svg":
                problems.append(("FAIL", name,
                                 "%s: root element is <%s>, expected <svg>"
                                 % (name, root.tag.split("}")[-1])))
                broken = True
                continue
            dw, dh = root.get("width"), root.get("height")
            if dw is None or dh is None:
                problems.append(("FAIL", name,
                                 "%s: <svg>%s declares no width/height, so the "
                                 "card cannot place it" % (name, tag)))
                broken = True
                continue
            if root.get("preserveAspectRatio") != "none":
                problems.append((
                    "WARN", name, "%s: <svg>%s has no preserveAspectRatio='none' — the "
                    "drawing will be letterboxed inside the card rather than "
                    "filling it" % (name, tag)))
            sizes.append((tag, dw, dh))

        # the repeated marks live inside CONCATENATEX, so they are not part of
        # the document skeleton above — each is parsed on its own, wrapped, or
        # an unbalanced tag in a mark body goes unnoticed
        for where, frag in markup_fragments(dax):
            try:
                ET.fromstring(WRAP % frag)
            except ET.ParseError as ex:
                problems.append((
                    "FAIL", name, "%s (%s): the %s does not parse — %s\n        %s"
                    % (name, rel, where, ex, _around(WRAP % frag, ex))))
                broken = True
        if broken:
            continue

        hosts = [c for c in cards if c[0] == name]
        if not hosts:
            problems.append(("WARN", name, "%s: no card hosts this measure" % name))
        for _n, page, vis, _t, cw, ch in hosts:
            for tag, dw, dh in sizes:
                for axis, declared, actual in (("width", dw, cw), ("height", dh, ch)):
                    if actual is None:
                        continue
                    try:
                        delta = abs(float(declared) - float(actual))
                    except (TypeError, ValueError):
                        delta = None
                    if delta is None or delta > EPS:
                        problems.append((
                            "FAIL", name, "%s: svg%s %s=%s but its card (%s/%s) is %s"
                            "%s — the drawing is stretched, not cropped, so "
                            "every mark lands off its grid"
                            % (name, tag, axis, declared, page, vis, actual,
                               "" if delta is None else " (off by %g)" % delta)))
    return problems, measures, cards, skipped


def _around(svg, ex, span=60):
    """The text around a parse error, so the failure is actionable."""
    try:
        line, col = ex.position
    except Exception:
        return ""
    at = sum(len(l) + 1 for l in svg.split("\n")[:line - 1]) + col
    return "..." + svg[max(0, at - span):at + span].replace("\n", " ") + "..."


def resolve(path):
    """(model_dir, report_dir) pairs from a project folder, a .SemanticModel,
    or a parent folder to sweep."""
    path = os.path.abspath(path)
    if path.endswith(".SemanticModel") and os.path.isdir(path):
        models = [path]
    else:
        models = sorted(glob.glob(os.path.join(path, "*.SemanticModel"))) or \
                 sorted(glob.glob(os.path.join(path, "*", "*.SemanticModel")))
    out = []
    for m in models:
        report = m[:-len(".SemanticModel")] + ".Report"
        out.append((m, report if os.path.isdir(report) else None))
    return out


def main():
    args = sys.argv[1:] or ["."]
    projects = []
    for a in args:
        projects += resolve(a)
    if not projects:
        print("no .SemanticModel folder found under: %s" % ", ".join(args))
        return 1

    fail = warn = total_m = total_c = 0
    for model_dir, report_dir in projects:
        print("\n===== %s =====" % model_dir)
        if report_dir is None:
            print("  WARN  no matching .Report folder — card sizes not checked")
            warn += 1
        problems, measures, cards, skipped = check_project(model_dir, report_dir)
        total_m += len(measures) - len(skipped)
        total_c += len(cards)
        bad = {m for sev, m, _msg in problems if sev == "FAIL" and m}
        for name in sorted(measures):
            if name in skipped:
                print("  --: %s (ImageUrl, but not an SVG measure)" % name)
            elif name not in bad:
                print("  ok: %s" % name)
        for sev, _m, msg in problems:
            print("  %-5s %s" % (sev, msg))
            if sev == "FAIL":
                fail += 1
            else:
                warn += 1

    if fail:
        print("\n%d SVG MEASURE FAILURE(S) — %d warning(s). Fix these before "
              "opening Power BI: each is a card that renders wrong and says "
              "nothing — blank for a broken document or a bad escape, "
              "distorted for a size mismatch." % (fail, warn))
    else:
        print("\nSVG MEASURES OK — %d measure(s), %d card(s), %d warning(s)"
              % (total_m, total_c, warn))
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
