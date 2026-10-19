"""
pbir_editor.py — Python library for programmatic Power BI PBIR report editing.  v2.0

Requires the report to be saved as a Power BI Project (.pbip) first (or scaffolded
via pbip_scaffold.py — no Power BI needed).

PBIR folder structure written by this library:
  <name>.Report/
    definition/
      report.json                  (only touched by embed_theme())
      pages/
        pages.json                 (touched by add_page()/remove_page())
        <page-id>/
          page.json
          visuals/
            <visual-id>/
              visual.json
    StaticResources/
      RegisteredResources/<theme>.json   (written by embed_theme())

v2 changes
----------
* NON-DESTRUCTIVE saves: get_page() loads existing visuals from disk; save()
  rewrites the full set. Only clear_visuals()/remove_visual() delete anything.
* PBI-open guard: save() raises if PBIDesktop.exe is running (silent-loss
  protection). Override with save(force=True).
* Canonical literal encoding: numbers carry the D suffix ("9D"), colors are
  wrapped in {"solid": {"color": ...}} — matches what Power BI Desktop itself
  writes. Fixes the "visual.objects properties not applying" issue.
* Apostrophes in titles/values are escaped ('' doubling).
* Validation: field names are checked against the sibling .SemanticModel TMDL
  files before writing. Typos fail fast with did-you-mean suggestions.
  Disable with PbirEditor(..., validate=False).
* Cross-table fields: any column/measure parameter accepts ("other_table", "name").
* New visuals: add_slicer(), add_text_box(), add_multi_row_card().
* add_page()/remove_page() — create/delete pages and maintain pages.json.
* set_canvas_size() + auto-grow: save() expands page height/width if visuals
  overflow the canvas.
* KPI cards now default to the classic "card" visual (themes style it — 36pt
  callout). Pass card_type="new" for the modern cardVisual.
* series_colors= on category charts: per-category hex colors via dataPoint
  selectors (e.g. user_status semantic colors — see STATUS_COLORS).
* embed_theme(style): registers the matching theme JSON inside the report so
  it applies automatically on open — no View->Themes clicking.
* build_from_spec(spec): build entire dashboards from a declarative dict/JSON.

v2.1 changes (competition backgrounds — Phase 1)
------------------------------------------------
* page.generate_background(): draws a designed page background PNG (header
  band + nav rail + rounded card slots with soft shadows) from the queued
  visuals' own positions (bg_generator.py / Pillow), registers it, wires
  page.json objects.background, and flips visuals to transparent. The
  standard technique behind competition-winning dashboards, no Figma needed.
* page.set_background(image_path): use any existing image as page background.
* build_from_spec: per-page "background": true | {"title", "subtitle",
  "nav_rail", ...}.
* STYLES entries gained "page_bg".
"""

import os, json, shutil, subprocess, uuid, difflib, inspect

__version__ = "2.1"

# ── Schema URLs (match Power BI Desktop output as of 2026) ────────────────────
# NOTE: PBI Desktop 2026 writes visualContainer 2.10.0 on save, but Microsoft has
# NOT published that schema (404) — stay on the newest PUBLISHED version so
# _validate_pbir can verify our files. The validator falls back gracefully for
# PBI-saved 2.10.0 files.
SCHEMA_VISUAL = "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/visualContainer/2.9.0/schema.json"
SCHEMA_PAGE   = "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/page/2.1.0/schema.json"
SCHEMA_PAGES  = "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/pagesMetadata/1.1.0/schema.json"

THEMES_DIR = os.environ.get(
    "PBIMAKE_THEMES_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "themes"))

# ── Aggregation function codes ─────────────────────────────────────────────────
# PBIR semantic-query enum, render-verified 2026-07-17: a chart built with
# code 1 showed "Average of <col>" in its tooltip (values = per-row average),
# while code 5 has always rendered as Count. The enum is:
#   0 Sum · 1 Avg · 2 Count · 3 Min · 4 Max · 5 CountNonNull · 6 Median
AGG_SUM    = 0
AGG_AVG    = 1
AGG_MIN    = 3
AGG_MAX    = 4
AGG_COUNT  = 5   # CountNonNull -- standard "Count"
AGG_COUNTD = 2   # Count (distinct-ish legacy slot; verify before relying)

_AGG_MAP = {
    "sum": AGG_SUM, "average": AGG_AVG, "avg": AGG_AVG,
    "min": AGG_MIN, "max": AGG_MAX, "count": AGG_COUNT,
    "countdistinct": AGG_COUNTD, "distinct": AGG_COUNTD,
}
_AGG_NAME = {
    AGG_SUM: "Sum", AGG_AVG: "Average", AGG_MIN: "Min",
    AGG_MAX: "Max", AGG_COUNT: "CountNonNull", AGG_COUNTD: "CountDistinct",
}

# ── Style presets ──────────────────────────────────────────────────────────────
STYLES = {
    "corporate_light": {
        "page_bg":        "#F3F2F1",
        "card_bg":        "#FFFFFF",
        "text_primary":   "#252423",
        "text_secondary": "#605E5C",
        "border":         "#E1DFDD",
        "accent":         "#0078D4",
        "muted":          "#C8D0DA",
        "tile_colors":    ["#0078D4","#107C10","#D83B01","#8764B8","#008575","#C239B3"],
        "data_colors":    ["#0078D4","#107C10","#D83B01","#FFB900",
                           "#008575","#8764B8","#4DA6FF","#881798"],
        "positive":       "#107C10",
        "negative":       "#D13438",
        "neutral":        "#FFB900",
    },
    "dark_analytics": {
        "page_bg":        "#1A2033",
        "card_bg":        "#242B3D",
        "text_primary":   "#F0F0F0",
        "text_secondary": "#A0A8BC",
        "border":         "#3A4260",
        "accent":         "#00B4D8",
        "muted":          "#4F5A7A",
        "tile_colors":    ["#4361EE","#06D6A0","#FF9F1C","#9B5DE5","#00B4D8","#F72585"],
        "data_colors":    ["#00B4D8","#4CC9F0","#F72585","#7209B7",
                           "#3A86FF","#FFBE0B","#06D6A0","#FB5607"],
        "positive":       "#06D6A0",
        "negative":       "#F72585",
        "neutral":        "#FFBE0B",
    },
    "minimal_neutral": {
        "page_bg":        "#FAFAFA",
        "card_bg":        "#FFFFFF",
        "text_primary":   "#1A1A1A",
        "text_secondary": "#6B7280",
        "border":         "#E5E7EB",
        "accent":         "#2D6A4F",
        "muted":          "#D1D5DB",
        "tile_colors":    ["#2D6A4F","#1C4E80","#E76F51","#6D597A","#2A9D8F","#B5838D"],
        "data_colors":    ["#2D6A4F","#52B788","#95D5B2","#1C4E80",
                           "#B5838D","#6B7280","#F4A261","#264653"],
        "positive":       "#2D6A4F",
        "negative":       "#C0392B",
        "neutral":        "#F4A261",
    },
}
DEFAULT_STYLE = "corporate_light"

# Brand styles created by brand_styles.create_brand_style() persist here and
# load on import, so style="acme" works in every later session.
_BRAND_STORE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "_data", "brand_styles.json")
if os.path.exists(_BRAND_STORE):
    try:
        with open(_BRAND_STORE, encoding="utf-8") as _f:
            for _name, _st in json.load(_f).items():
                STYLES.setdefault(_name, _st)
    except (OSError, ValueError) as _e:
        print(f"  [warn] brand styles not loaded ({_e})")

# Visual types treated as KPI-family by generate_background() (accent bar slots)
KPI_TYPES = {"card", "cardVisual", "multiRowCard", "gauge", "kpi"}

# Friendly names for auto-generated alt text (accessibility)
_FRIENDLY_TYPE = {
    "clusteredBarChart": "Bar chart", "barChart": "Stacked bar chart",
    "clusteredColumnChart": "Column chart", "columnChart": "Stacked column chart",
    "lineChart": "Line chart", "areaChart": "Area chart",
    "pieChart": "Pie chart", "donutChart": "Donut chart", "funnel": "Funnel chart",
    "treemap": "Treemap", "waterfallChart": "Waterfall chart",
    "scatterChart": "Scatter chart", "lineClusteredColumnComboChart": "Combo chart",
    "card": "KPI card", "cardVisual": "KPI card", "multiRowCard": "Multi-value card",
    "gauge": "Gauge", "tableEx": "Table", "matrix": "Matrix", "slicer": "Slicer",
}

# Visual types that float on the background with no card slot drawn for them
NO_SLOT_TYPES = {"textbox", "basicShape", "shape", "image", "actionButton", "pageNavigator"}


def _no_slot(v: dict) -> bool:
    """True for visuals drawn straight on the background (no card slot, never
    shifted below the header): NO_SLOT_TYPES, plus any visual whose name starts
    with "ns_" — header controls (slicer, button) and menu-footer text. A spec
    entry sets it with "slot": false."""
    return (v["visual"]["visualType"] in NO_SLOT_TYPES
            or v.get("name", "").startswith("ns_"))

# Semantic colors for user_status / revenue-movement fields.
# Usage: series_colors=STATUS_COLORS on any category chart.
STATUS_COLORS = {
    "RETAINED":    "#27AE60",
    "NEW":         "#2F80ED",
    "EXPANSION":   "#56CCF2",
    "REACTIVATED": "#9B51E0",
    "CHURNED":     "#EB5757",
    "CONTRACTION": "#F2994A",
}


# ── Process guard ──────────────────────────────────────────────────────────────

def powerbi_is_open() -> bool:
    """True if PBIDesktop.exe is currently running."""
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq PBIDesktop.exe", "/NH"],
            capture_output=True, text=True, timeout=15,
        ).stdout
        return "PBIDesktop.exe" in out
    except Exception:
        return False


# ── Literal / expression encoding (matches Power BI Desktop output) ───────────

def _esc(s: str) -> str:
    """Escape single quotes for SemanticQuery string literals."""
    return str(s).replace("'", "''")

def _lit_str(v: str) -> dict:
    return {"expr": {"Literal": {"Value": f"'{_esc(v)}'"}}}

def _lit_bool(v: bool) -> dict:
    return {"expr": {"Literal": {"Value": "true" if v else "false"}}}

def _lit_num(v) -> dict:
    n = f"{v:g}" if isinstance(v, float) else str(v)
    return {"expr": {"Literal": {"Value": f"{n}D"}}}

def _lit_int(v) -> dict:
    """Integer literal — L suffix (PBI writes shape rotation etc. this way)."""
    return {"expr": {"Literal": {"Value": f"{int(v)}L"}}}

def _solid(color: str) -> dict:
    """Color property — PBI wraps colors in solid/color."""
    return {"solid": {"color": {"expr": {"Literal": {"Value": f"'{_esc(color)}'"}}}}}


def _solid_measure(entity: str, measure: str) -> dict:
    """Color property driven by a measure that returns a hex string — Power BI's
    'Format style: Field value' conditional formatting (fx)."""
    return {"solid": {"color": {"expr": {"Measure": {
        "Expression": {"SourceRef": {"Entity": entity}}, "Property": measure}}}}}


def _data_point_measure_fill(entity: str, measure: str) -> list:
    """Per-bar fill from a color measure. The dataViewWildcard selector
    (matchingOption 1 = every instance) is what Power BI writes for fx fills."""
    return [{
        "properties": {"fill": _solid_measure(entity, measure)},
        "selector": {"data": [{"dataViewWildcard": {"matchingOption": 1}}]},
    }]


# ── Field projection helpers ───────────────────────────────────────────────────

def _uid() -> str:
    return uuid.uuid4().hex[:20]

def _split_ref(default_table: str, ref):
    """Accept "name" (uses default table) or ("table", "name") for cross-table fields."""
    if isinstance(ref, (tuple, list)) and len(ref) == 2:
        return ref[0], ref[1]
    return default_table, ref

def _measure_field(table: str, measure: str) -> dict:
    return {
        "field": {"Measure": {
            "Expression": {"SourceRef": {"Entity": table}},
            "Property": measure,
        }},
        "queryRef": f"{table}.{measure}",
        "nativeQueryRef": measure,
    }

def _sparkline_field(table: str, measure: str, date_table: str, date_col: str,
                     display_name: str = None) -> dict:
    """Table/matrix sparkline column (ground-truthed 2026-09-26: UI "Add a
    sparkline" -> save -> read)."""
    return {
        "field": {"SparklineData": {
            "Measure": {"Measure": {"Expression": {"SourceRef": {"Entity": table}},
                                    "Property": measure}},
            "Groupings": [{"Column": {"Expression": {"SourceRef": {"Entity": date_table}},
                                      "Property": date_col}}],
            "ApplyCalculationGroupTo": "Point",
        }},
        "queryRef": f"SparklineData({table}.{measure}_[{date_table}.{date_col}])",
        "nativeQueryRef": display_name or f"{measure} by {date_col}",
        **({"displayName": display_name} if display_name else {}),
    }


def _trend_icon_rule(table: str, column_measure: str, change_table: str,
                     change_measure: str) -> dict:
    """values.icon conditional formatting: ▲ green / ▼ red / → neutral by the
    SIGN of a change measure (design_measures.add_kpi_context "change"). Same
    Conditional/Cases encoding Power BI writes for icon rules (ground-truthed
    2026-09-26); comparisons against 0 instead of RangePercent."""
    chg = {"Measure": {"Expression": {"SourceRef": {"Entity": change_table}},
                       "Property": change_measure}}
    zero = {"Literal": {"Value": "0D"}}

    def case(kind, icon):
        return {"Condition": {"Comparison": {"ComparisonKind": kind, "Left": chg,
                                             "Right": zero}},
                "Value": {"Literal": {"Value": f"'{icon}'"}}}
    return {
        "properties": {"icon": {
            "kind": "Icon",
            "layout": _lit_str("Before"),
            "verticalAlignment": _lit_str("Top"),    # value seen in PBI output
            "value": {"expr": {"Conditional": {"Cases": [
                case(1, "ColoredArrowUp"),        # >  0
                case(3, "ColoredArrowDown"),      # <  0
                case(0, "ColoredArrowRight"),     # == 0
            ]}}},
        }},
        "selector": {"data": [{"dataViewWildcard": {"matchingOption": 1}}],
                     "metadata": f"{table}.{column_measure}"},
    }


def _column_field(table: str, column: str) -> dict:
    return {
        "field": {"Column": {
            "Expression": {"SourceRef": {"Entity": table}},
            "Property": column,
        }},
        "queryRef": f"{table}.{column}",
        "nativeQueryRef": column,
    }

def _agg_field(table: str, column: str, agg_func: int = AGG_COUNT) -> dict:
    agg_name = _AGG_NAME.get(agg_func, "CountNonNull")
    display  = f"Count of {column}" if agg_func == AGG_COUNT else f"{agg_name} of {column}"
    return {
        "field": {"Aggregation": {
            "Function": agg_func,
            "Expression": {"Column": {
                "Expression": {"SourceRef": {"Entity": table}},
                "Property": column,
            }},
        }},
        "queryRef": f"{agg_name}({table}.{column})",
        "nativeQueryRef": display,
    }

def _resolve_agg(agg: str) -> int:
    key = agg.lower().strip()
    if key not in _AGG_MAP:
        raise ValueError(f"Unknown aggregation '{agg}'. Choose from: {list(_AGG_MAP)}")
    return _AGG_MAP[key]


# ── Semantic model index (validation) ──────────────────────────────────────────

class ModelIndex:
    """
    Parses the .SemanticModel TMDL table files to know which tables, columns and
    measures actually exist. Used by PbirEditor to fail fast on typos instead of
    writing a visual that renders blank.
    """

    def __init__(self, semantic_model_folder: str):
        self.folder = semantic_model_folder
        self.tables = {}          # name -> {"columns": set, "measures": set}
        self._load()

    @staticmethod
    def _parse_decl(line: str, keyword: str):
        s = line.strip()
        if not s.startswith(keyword + " "):
            return None
        rest = s[len(keyword) + 1:].strip()
        if rest.startswith("'"):
            end = rest.find("'", 1)
            while end != -1 and end + 1 < len(rest) and rest[end + 1] == "'":
                end = rest.find("'", end + 2)
            return rest[1:end].replace("''", "'") if end != -1 else None
        # unquoted: name ends at first whitespace or '='
        for i, ch in enumerate(rest):
            if ch in (" ", "\t", "="):
                return rest[:i]
        return rest

    def _load(self):
        tables_dir = os.path.join(self.folder, "definition", "tables")
        if not os.path.isdir(tables_dir):
            return
        for fname in os.listdir(tables_dir):
            if not fname.endswith(".tmdl"):
                continue
            path = os.path.join(tables_dir, fname)
            with open(path, encoding="utf-8-sig") as f:
                text = f.read()
            tname, cols, meas = None, set(), set()
            for line in text.splitlines():
                if line.startswith("table "):
                    tname = self._parse_decl(line, "table")
                elif line.startswith("\tcolumn "):
                    c = self._parse_decl(line, "column")
                    if c:
                        cols.add(c)
                elif line.startswith("\tmeasure "):
                    m = self._parse_decl(line, "measure")
                    if m:
                        meas.add(m)
            if tname:
                self.tables[tname] = {"columns": cols, "measures": meas}

    # -- lookups -----------------------------------------------------------------

    def has_table(self, table: str) -> bool:
        return table in self.tables

    def measure_home(self, measure: str):
        """Return the table that owns a measure, or None."""
        for t, d in self.tables.items():
            if measure in d["measures"]:
                return t
        return None

    def _suggest(self, name: str, pool) -> str:
        m = difflib.get_close_matches(name, list(pool), n=1)
        return f" Did you mean '{m[0]}'?" if m else ""

    def check_table(self, table: str):
        if not self.has_table(table):
            raise ValueError(
                f"Table '{table}' not found in the semantic model."
                f"{self._suggest(table, self.tables)} "
                f"Available: {sorted(t for t in self.tables if not t.startswith(('LocalDateTable_', 'DateTableTemplate_')))}"
            )

    def check_column(self, table: str, column: str):
        self.check_table(table)
        cols = self.tables[table]["columns"]
        if column not in cols:
            raise ValueError(
                f"Column '{column}' not found in table '{table}'."
                f"{self._suggest(column, cols)} Available: {sorted(cols)}"
            )

    def check_measure(self, table: str, measure: str):
        self.check_table(table)
        if measure not in self.tables[table]["measures"]:
            home = self.measure_home(measure)
            if home:
                raise ValueError(
                    f"Measure '{measure}' lives in table '{home}', not '{table}'. "
                    f"Pass table='{home}' (or ('{home}', '{measure}'))."
                )
            pool = self.tables[table]["measures"]
            raise ValueError(
                f"Measure '{measure}' not found in table '{table}'."
                f"{self._suggest(measure, pool)} Available: {sorted(pool)} "
                "(create it first via tmdl_editor.py, then rebuild)"
            )


# ── Chart-level objects (visual.objects) ──────────────────────────────────────
# Canonical encoding: bools "true"/"false", numbers with D suffix, strings
# single-quoted — identical to what Power BI Desktop writes on save.
# The theme JSON sets the same defaults per visual type, so even a property
# PBI ignores here is still covered by the embedded theme.

def _objs_bar() -> dict:
    return {
        "labels":       [{"properties": {"show": _lit_bool(True),  "fontSize": _lit_num(9)}}],
        "categoryAxis": [{"properties": {"showAxisTitle": _lit_bool(False), "fontSize": _lit_num(9)}}],
        "valueAxis":    [{"properties": {"show": _lit_bool(False)}}],
        "legend":       [{"properties": {"show": _lit_bool(False)}}],
    }

def _objs_bar_series() -> dict:
    """Stacked bar/column -- legend on to show the series grouping."""
    objs = _objs_bar()
    objs["valueAxis"] = [{"properties": {"showAxisTitle": _lit_bool(False), "fontSize": _lit_num(9)}}]
    objs["legend"] = [{"properties": {
        "show": _lit_bool(True), "position": _lit_str("Top"), "fontSize": _lit_num(9),
    }}]
    return objs

def _objs_column() -> dict:
    return {
        "labels":       [{"properties": {"show": _lit_bool(True),  "fontSize": _lit_num(9)}}],
        "categoryAxis": [{"properties": {"showAxisTitle": _lit_bool(False), "fontSize": _lit_num(9)}}],
        "valueAxis":    [{"properties": {"showAxisTitle": _lit_bool(False), "fontSize": _lit_num(9)}}],
        "legend":       [{"properties": {"show": _lit_bool(False)}}],
    }

def _objs_line() -> dict:
    return {
        "labels":       [{"properties": {"show": _lit_bool(False)}}],
        "categoryAxis": [{"properties": {"showAxisTitle": _lit_bool(False), "fontSize": _lit_num(9)}}],
        "valueAxis":    [{"properties": {"showAxisTitle": _lit_bool(False), "fontSize": _lit_num(9)}}],
        "legend":       [{"properties": {
            "show": _lit_bool(True), "position": _lit_str("TopRight"), "fontSize": _lit_num(9),
        }}],
    }

def _objs_pie() -> dict:
    return {
        "legend":       [{"properties": {"show": _lit_bool(False)}}],
        "labels":       [{"properties": {"show": _lit_bool(True), "fontSize": _lit_num(9)}}],
    }

def _objs_funnel() -> dict:
    # funnel has no legend card — PBI strips a "legend" object on save
    return {
        "labels":       [{"properties": {"show": _lit_bool(True), "fontSize": _lit_num(9)}}],
        "categoryAxis": [{"properties": {"show": _lit_bool(False)}}],
    }

def _objs_scatter() -> dict:
    return {
        "labels":       [{"properties": {"show": _lit_bool(False)}}],
        "categoryAxis": [{"properties": {"showAxisTitle": _lit_bool(True), "fontSize": _lit_num(9)}}],
        "valueAxis":    [{"properties": {"showAxisTitle": _lit_bool(True), "fontSize": _lit_num(9)}}],
        "legend":       [{"properties": {
            "show": _lit_bool(True), "position": _lit_str("TopRight"), "fontSize": _lit_num(9),
        }}],
    }

def _objs_treemap() -> dict:
    return {
        "labels": [{"properties": {"show": _lit_bool(True), "fontSize": _lit_num(9)}}],
        "legend":     [{"properties": {"show": _lit_bool(False)}}],
    }

def _objs_combo() -> dict:
    """lineClusteredColumnComboChart -- legend on to distinguish bar vs line.
    (no valueAxis2 object — PBI strips it; secondary-axis styling is not supported)"""
    return {
        "labels":       [{"properties": {"show": _lit_bool(False)}}],
        "categoryAxis": [{"properties": {"showAxisTitle": _lit_bool(False), "fontSize": _lit_num(9)}}],
        "valueAxis":    [{"properties": {"showAxisTitle": _lit_bool(False), "fontSize": _lit_num(9)}}],
        "legend":       [{"properties": {
            "show": _lit_bool(True), "position": _lit_str("TopRight"), "fontSize": _lit_num(9),
        }}],
    }

def _objs_waterfall() -> dict:
    return {
        "labels":       [{"properties": {"show": _lit_bool(True), "fontSize": _lit_num(9)}}],
        "categoryAxis": [{"properties": {"showAxisTitle": _lit_bool(False), "fontSize": _lit_num(9)}}],
        "valueAxis":    [{"properties": {"showAxisTitle": _lit_bool(False), "fontSize": _lit_num(9)}}],
        "legend":       [{"properties": {
            "show": _lit_bool(True), "position": _lit_str("Top"), "fontSize": _lit_num(9),
        }}],
    }

def _data_point_objects(entity: str, column: str, color_map: dict) -> list:
    """Per-category fill colors via dataPoint selectors (e.g. STATUS_COLORS)."""
    pts = []
    for value, color in color_map.items():
        pts.append({
            "properties": {"fill": _solid(color)},
            "selector": {"data": [{"scopeId": {"Comparison": {
                "ComparisonKind": 0,
                "Left": {"Column": {
                    "Expression": {"SourceRef": {"Entity": entity}},
                    "Property": column,
                }},
                "Right": {"Literal": {"Value": f"'{_esc(str(value))}'"}},
            }}}]},
        })
    return pts


def _topn_filter(cat_table: str, cat_col: str,
                 by_table: str, by_measure: str, n: int) -> dict:
    """
    Visual-level Top-N filter entry (filterConfig) — replicates PBI's encoding
    exactly (ground-truthed 2026-07-03: UI Top-N -> save -> read). The category
    and the ranking measure may live in different tables (star schema).
    """
    aliases = [{"Name": "c", "Entity": cat_table, "Type": 0}]
    by_src = "c"
    if by_table != cat_table:
        aliases.append({"Name": "m", "Entity": by_table, "Type": 0})
        by_src = "m"
    return {
        "name": _uid(),
        "field": {"Column": {"Expression": {"SourceRef": {"Entity": cat_table}},
                             "Property": cat_col}},
        "type": "TopN",
        "filter": {
            "Version": 2,
            "From": [
                {"Name": "subquery", "Expression": {"Subquery": {"Query": {
                    "Version": 2,
                    "From": aliases,
                    "Select": [{"Column": {
                        "Expression": {"SourceRef": {"Source": "c"}},
                        "Property": cat_col}, "Name": "field"}],
                    "OrderBy": [{"Direction": 2, "Expression": {"Measure": {
                        "Expression": {"SourceRef": {"Source": by_src}},
                        "Property": by_measure}}}],
                    "Top": int(n),
                }}}, "Type": 2},
                {"Name": "c", "Entity": cat_table, "Type": 0},
            ],
            "Where": [{"Condition": {"In": {
                "Expressions": [{"Column": {
                    "Expression": {"SourceRef": {"Source": "c"}},
                    "Property": cat_col}}],
                "Table": {"SourceRef": {"Source": "subquery"}},
            }}}],
        },
    }


# ── Visual builder ─────────────────────────────────────────────────────────────

def _make_visual(name: str, x: int, y: int, w: int, h: int,
                 visual_type: str, query_state: dict,
                 title: str = None, z: int = 1000, tab_order: int = 0,
                 sort_field: dict = None, sort_desc: bool = True,
                 style: str = None, objects: dict = None,
                 background: bool = True) -> dict:

    s = STYLES.get(style or DEFAULT_STYLE, STYLES[DEFAULT_STYLE])

    query = {"queryState": query_state} if query_state else None
    if query and sort_field:
        query["sortDefinition"] = {
            "sort": [{"field": sort_field["field"],
                      "direction": "Descending" if sort_desc else "Ascending"}],
            "isDefaultSort": False,
        }

    v = {
        "$schema": SCHEMA_VISUAL,
        "name": name,
        "position": {"x": x, "y": y, "z": z, "height": h, "width": w, "tabOrder": tab_order},
        "visual": {
            "visualType": visual_type,
            "drillFilterOtherVisuals": True,
        },
    }
    if query:
        v["visual"]["query"] = query

    # Container objects: title + card background color (from style)
    container = {}
    if title:
        container["title"] = [{"properties": {
            "show": _lit_bool(True),
            "text": _lit_str(title),
        }}]
        # auto alt text (accessibility scorecard dimension) — override with
        # page.set_alt_text(). Encoding ground-truthed 2026-07-03.
        friendly = _FRIENDLY_TYPE.get(visual_type, visual_type)
        container["general"] = [{"properties": {
            "altText": _lit_str(f"{friendly}: {title}"),
        }}]
    if background:
        container["background"] = [{"properties": {
            "show": _lit_bool(True),
            "color": _solid(s["card_bg"]),
            "transparency": _lit_num(0),
        }}]
    else:
        container["background"] = [{"properties": {"show": _lit_bool(False)}}]
    v["visual"]["visualContainerObjects"] = container

    # Chart-level formatting (axes, data labels, legend, dataPoint colors)
    if objects:
        v["visual"]["objects"] = objects

    return v


def _visual_title(v: dict):
    """Extract the title text of a visual dict, or None."""
    try:
        raw = v["visual"]["visualContainerObjects"]["title"][0]["properties"]["text"]["expr"]["Literal"]["Value"]
        return raw[1:-1].replace("''", "'") if raw.startswith("'") else raw
    except (KeyError, IndexError, TypeError):
        return None


def _tile_container(v: dict, title_off: bool = False, alt: str = None,
                    padding: tuple = (0, 0, 0, 0)) -> None:
    """Container settings for KPI-tile parts. The parts sit INSIDE one card
    slot, so anything the theme draws per visual (drop shadow, padding, hover
    header) shows up as a box-within-a-box — switch all of it off."""
    top, right, bottom, left = padding
    vco = v["visual"].setdefault("visualContainerObjects", {})
    vco["dropShadow"] = [{"properties": {"show": _lit_bool(False)}}]
    vco["border"] = [{"properties": {"show": _lit_bool(False)}}]
    vco["padding"] = [{"properties": {"top": _lit_num(top), "right": _lit_num(right),
                                      "bottom": _lit_num(bottom), "left": _lit_num(left)}}]
    vco["visualHeader"] = [{"properties": {"show": _lit_bool(False)}}]
    if title_off:
        vco["title"] = [{"properties": {"show": _lit_bool(False)}}]
    if alt:
        vco["general"] = [{"properties": {"altText": _lit_str(alt)}}]


# ── Page class ─────────────────────────────────────────────────────────────────

class PbirPage:
    """
    Represents one page in a PBIR report.

    Existing visuals on disk are loaded automatically — save() preserves them
    unless you call clear_visuals() or remove_visual() first.

    style parameter accepted by all add_* methods:
        "corporate_light"  -- white cards, blue accent  (default)
        "dark_analytics"   -- navy cards, teal/cyan accent
        "minimal_neutral"  -- white cards, sage green accent
    """

    def __init__(self, page_dir: str, page_id: str, display_name: str,
                 width: int = 1280, height: int = 720,
                 page_json: dict = None, editor=None):
        self.page_dir     = page_dir
        self.page_id      = page_id
        self.display_name = display_name
        self.width        = width
        self.height       = height
        self._page_json   = page_json or {}
        self._editor      = editor
        self._visuals: list = []
        self._tiles: dict = {}        # kpi tile group id -> slot rect + icon
        self._load_existing()

    # ── infrastructure ─────────────────────────────────────────────────────────

    def _load_existing(self):
        vdir = os.path.join(self.page_dir, "visuals")
        if not os.path.isdir(vdir):
            return
        for folder in sorted(os.listdir(vdir)):
            vpath = os.path.join(vdir, folder, "visual.json")
            if os.path.exists(vpath):
                with open(vpath, encoding="utf-8") as f:
                    self._visuals.append(json.load(f))

    def _model(self):
        return self._editor.model_index if (self._editor and self._editor.validate) else None

    def _col(self, table: str, ref) -> dict:
        t, c = _split_ref(table, ref)
        m = self._model()
        if m:
            m.check_column(t, c)
        return _column_field(t, c)

    def _meas(self, table: str, ref) -> dict:
        t, name = _split_ref(table, ref)
        m = self._model()
        if m:
            m.check_measure(t, name)
        return _measure_field(t, name)

    def _aggf(self, table: str, ref, agg: str) -> dict:
        t, c = _split_ref(table, ref)
        m = self._model()
        if m:
            m.check_column(t, c)
        return _agg_field(t, c, _resolve_agg(agg))

    def _yf(self, table: str, value_col, agg: str, measure) -> dict:
        """Y field: either a DAX measure or an aggregated column."""
        if measure:
            return self._meas(table, measure)
        if value_col:
            return self._aggf(table, value_col, agg)
        raise ValueError("Provide either value_col+agg or measure=")

    def _series_objs(self, base_objs: dict, table: str, column_ref,
                     series_colors: dict, color_measure=None) -> dict:
        if series_colors and color_measure:
            raise ValueError("Pass series_colors= OR color_measure=, not both")
        if color_measure:
            t, m = _split_ref(table, color_measure)
            self._meas(table, color_measure)          # validates it exists
            objs = dict(base_objs or {})
            objs["dataPoint"] = _data_point_measure_fill(t, m)
            return objs
        if not series_colors:
            return base_objs
        t, c = _split_ref(table, column_ref)
        objs = dict(base_objs or {})
        objs["dataPoint"] = _data_point_objects(t, c, series_colors)
        return objs

    # ── KPI & Gauge ────────────────────────────────────────────────────────────

    def add_card_measure(self, title: str, table: str, measure: str,
                         x: int = 20, y: int = 20, w: int = 390, h: int = 120,
                         style: str = None, z: int = 1000,
                         card_type: str = "classic",
                         show_category_label: bool = None) -> None:
        """
        KPI card bound to a DAX measure (must exist in the SemanticModel).

        card_type: "classic" (default; themes style it — 36pt callout value)
                   or "new" (modern cardVisual).
        Display formatting ($, %, decimals) comes from the measure's formatString —
        set it when creating the measure (tmdl_editor add_measure format_string=).

        show_category_label: the small caption Power BI prints UNDER the number,
            which is the measure's own name. Default None resolves to False when
            the card has a title and True when it does not — because a titled
            card otherwise prints its name twice:

                Leads received      <- container title
                     42K            <- callout value (objects.labels)
                Leads received      <- category label (objects.categoryLabels)

            Titles are written for humans ("Closure rate") and measure names
            usually match them, so the caption is pure duplication. Pass True
            only when the caption says something the title does not.
        """
        mf = self._meas(table, measure)
        vt = "card" if card_type == "classic" else "cardVisual"
        role = "Values" if card_type == "classic" else "Data"
        if show_category_label is None:
            show_category_label = not title
        objects = None
        if vt == "card" and not show_category_label:
            objects = {"categoryLabels": [{"properties": {"show": _lit_bool(False)}}]}
        self._visuals.append(_make_visual(
            _uid(), x, y, w, h, vt,
            query_state={role: {"projections": [mf]}},
            title=title, style=style, z=z, objects=objects,
        ))

    def add_svg_card(self, table: str, measure: str,
                     x: int = 20, y: int = 20, w: int = 348, h: int = 119,
                     z: int = 1000, alt: str = None, slot: bool = False,
                     image_fit: str = "Fit", name: str = None) -> None:
        """
        A measure that returns an SVG document, rendered as the card's image —
        the whole visual IS the drawing (svg_measures composes the measure).

        The card is stripped of everything Power BI would otherwise draw: no
        callout value, no label, no background, border, shadow, header, title
        or tooltip, and zero padding on every layer. Anything left on would
        show through as chrome around a drawing that already has its own.

            card = svg_measures.kpi_sparkbar(348, 119, "Hires", ...)
            model.add_measure("_Measures", "KPI hires (SVG)", card.dax,
                              text_measure=True, data_category="ImageUrl")
            page.add_svg_card("_Measures", "KPI hires (SVG)", 40, 100,
                              card.width, card.height)

        The measure MUST carry dataCategory: ImageUrl, and the SVG's declared
        width/height must equal w/h — _validate_svg_measures.py gates both.

        slot=False (default) names the visual "ns_...", so generate_background()
        does not draw a card slot behind a card that paints its own.
        """
        mf = self._meas(table, measure)
        ent = mf["field"]["Measure"]["Expression"]["SourceRef"]["Entity"]
        prop = mf["field"]["Measure"]["Property"]
        mref = {"expr": {"Measure": {"Expression": {"SourceRef": {"Entity": ent}},
                                     "Property": prop}}}
        off = [{"properties": {"show": _lit_bool(False)}}]
        objects = {
            # the value and the label are the card's own text layers — the
            # drawing replaces both
            "value": [{"properties": {"show": _lit_bool(False)}},
                      {"properties": {"show": _lit_bool(False)},
                       "selector": {"id": "default"}}],
            "label": [{"properties": {"show": _lit_bool(False)},
                       "selector": {"id": "default"}}],
            "layout": [
                {"properties": {"backgroundShow": _lit_bool(False),
                                "topOuterMargin": _lit_int(0),
                                "bottomOuterMargin": _lit_int(0),
                                "leftOuterMargin": _lit_int(0),
                                "rightOuterMargin": _lit_int(0),
                                "paddingUniform": _lit_int(0)},
                 "selector": {"id": "default"}},
                # calloutSize 0 gives the image the whole card; without it the
                # drawing is squeezed into whatever the callout leaves
                {"properties": {"contentOrder": _lit_str("image_referenceLabel_callout"),
                                "calloutSize": _lit_num(0)}},
            ],
            "cardCalloutArea": [{"properties": {"paddingUniform": _lit_int(0),
                                                "paddingIndividual": _lit_bool(False)},
                                 "selector": {"id": "default"}}],
            "padding": [{"properties": {"paddingUniform": _lit_int(0),
                                        "paddingIndividual": _lit_bool(False)},
                         "selector": {"id": "default"}}],
            "fillCustom": [{"properties": {"show": _lit_bool(False)}}],
            "outline": [{"properties": {"show": _lit_bool(False)},
                         "selector": {"id": "default"}}],
            "divider": [{"properties": {"show": _lit_bool(False)},
                         "selector": {"id": "default"}}],
            "cardImage": [{"properties": {
                "show": _lit_bool(True),
                "imageType": _lit_str("imageData"),
                "imageData": mref,
                "imageDataFit": _lit_str(image_fit),
                "imageHeight": _lit_int(round(h)),
            }, "selector": {"id": "default"}}],
        }
        v = _make_visual(
            name or _uid(), x, y, w, h, "cardVisual",
            query_state={"Data": {"projections": [mf]}},
            title=None, z=z, objects=objects, background=False,
        )
        vco = v["visual"]["visualContainerObjects"]
        for key in ("visualHeader", "title", "background", "border", "dropShadow",
                    "visualTooltip"):
            vco[key] = list(off)
        vco["padding"] = [{"properties": {"top": _lit_num(0), "bottom": _lit_num(0),
                                          "left": _lit_num(0), "right": _lit_num(0)}}]
        vco["general"] = [{"properties": {
            "altText": _lit_str(alt or f"Chart: {measure}")}}]
        if not slot and not v["name"].startswith("ns_"):
            v["name"] = "ns_" + v["name"][:17]
        self._visuals.append(v)

    def add_kpi_tile(self, title: str, table: str, measure: str,
                     note_measure=None, note_color_measure=None,
                     trend_col=None, trend_measure=None,
                     icon: str = None, color: str = None, index: int = 0,
                     display_units: str = "none", value_font_size: int = 26,
                     x: int = 20, y: int = 110, w: int = 300, h: int = 160,
                     style: str = None, z: int = 1000) -> None:
        """
        Rich KPI tile — the card anatomy the top-rated marketplace dashboards
        share: colored icon tile, label + value, a colored context line
        ("▲ 12.3% Aug vs Jul"), and a small trend line along the bottom.

            ┌──────────────────────────────┐
            │ [■]      Applications        │   icon tile baked into the background
            │            4,300             │   value card (classic card)
            │     ▼ 10.5% Aug vs Jul       │   note card (text measure, fx color)
            │ ▁▂▃▅▃▂▄▆▇                    │   trend (area chart, no axes)
            └──────────────────────────────┘

        Built from up to three visuals that share ONE background slot, so it
        needs generate_background() (or a spec "background") to read as a card.
        The measures come from design_measures.add_kpi_context():
            note_measure       -> ctx["label"]   (text, e.g. "▲ 4.2% Aug vs Jul")
            note_color_measure -> ctx["color"]   (hex; green/red by direction)
            trend_measure      -> ctx["trend"]   (complete months only)
        Any text measure works as a note ("5.2% of applications").

        icon:  glyph name from icon_generator.GLYPHS, drawn white on a tile
        color: tile + trend color; default = style tile_colors[index]
        display_units: "none" (default) shows the measure's own format string;
            the classic card otherwise auto-rounds (EUR 1,740,783 -> EUR 2M).
        """
        s = STYLES.get(style or DEFAULT_STYLE, STYLES[DEFAULT_STYLE])
        tiles = s.get("tile_colors") or [s["accent"]]
        color = color or tiles[index % len(tiles)]
        if trend_measure and not trend_col:
            raise ValueError("trend_measure= needs trend_col= (the month column)")
        if note_color_measure and not note_measure:
            raise ValueError("note_color_measure= needs note_measure=")

        gid = uuid.uuid4().hex[:10]
        pad, icon_px = 14, 36
        has_note, has_trend = bool(note_measure), bool(trend_col)
        # value gets a fixed band; the trend takes what is left. Power BI swaps
        # a chart for its type icon when it gets too small, so the trend strip
        # must stay >= ~50px (and carries no padding — see _tile_container).
        value_h = 70
        note_h = 26 if has_note else 0
        trend_h = h - 4 - value_h - note_h - 4 if has_trend else 0
        if has_trend and trend_h < 50:
            raise ValueError(f"kpi tile h={h} leaves a {trend_h}px trend strip; "
                             "Power BI collapses charts that small — use h >= "
                             f"{h + 50 - trend_h}")
        if not has_trend:
            value_h = h - 8 - note_h

        # 1) value — sits right of the icon tile
        vx = x + pad + icon_px + 6 if icon else x + 8
        mf = self._meas(table, measure)
        label_props = {"fontSize": _lit_num(value_font_size),
                       "fontFamily": _lit_str("Segoe UI Semibold")}
        if display_units == "none":
            label_props["labelDisplayUnits"] = _lit_num(1)
        v = _make_visual(
            f"kt_{gid}_value", int(vx), int(y + 4), int(x + w - 8 - vx), int(value_h),
            "card", query_state={"Values": {"projections": [mf]}},
            title=title, style=style, z=z,
            objects={"labels": [{"properties": label_props}],
                     "categoryLabels": [{"properties": {"show": _lit_bool(False)}}]},
            background=False)
        _tile_container(v, padding=(2, 4, 0, 4))
        self._visuals.append(v)

        cursor = y + 4 + value_h
        # 2) note — one line of context, colored by a measure when given
        if has_note:
            nf = self._meas(table, note_measure)
            if note_color_measure:
                self._meas(table, note_color_measure)
                note_color = _solid_measure(*_split_ref(table, note_color_measure))
            else:
                note_color = _solid(s["text_secondary"])
            nv = _make_visual(
                f"kt_{gid}_note", int(x + 8), int(cursor), int(w - 16), note_h,
                "card", query_state={"Values": {"projections": [nf]}},
                style=style, z=z,
                objects={"labels": [{"properties": {
                            "fontSize": _lit_num(11), "color": note_color,
                            "fontFamily": _lit_str("Segoe UI Semibold")}}],
                         "categoryLabels": [{"properties": {"show": _lit_bool(False)}}]},
                background=False)
            _tile_container(nv, title_off=True, alt=f"{title}: context")
            self._visuals.append(nv)
            cursor += note_h

        # 3) trend — area chart stripped down to its shape
        if has_trend:
            tf = self._meas(table, trend_measure) if trend_measure else mf
            cf = self._col(table, trend_col)
            tv = _make_visual(
                f"kt_{gid}_trend", int(x + 6), int(cursor), int(w - 12),
                int(trend_h), "areaChart",
                {"Category": {"projections": [cf]}, "Y": {"projections": [tf]}},
                style=style, z=z,
                objects={
                    "categoryAxis": [{"properties": {"show": _lit_bool(False)}}],
                    "valueAxis":    [{"properties": {"show": _lit_bool(False),
                                                     "gridlineShow": _lit_bool(False)}}],
                    "legend":       [{"properties": {"show": _lit_bool(False)}}],
                    "labels":       [{"properties": {"show": _lit_bool(False)}}],
                    # series color is keyed to the field's queryRef — a fill
                    # with no selector is silently ignored (theme color wins)
                    "dataPoint":    [{"properties": {"fill": _solid(color)},
                                      "selector": {"metadata": tf["queryRef"]}}],
                },
                background=False)
            _tile_container(tv, title_off=True, alt=f"{title}: monthly trend")
            self._visuals.append(tv)

        self._tiles[gid] = {"x": x, "y": y, "w": w, "h": h,
                            "icon": icon, "color": color}

    def add_decomposition_tree(self, title: str, table: str, measure: str,
                               explain_by: list, expand: list = None,
                               bars_per_level: int = 5,
                               x: int = 20, y: int = 160, w: int = 590, h: int = 400,
                               style: str = None, z: int = 1000) -> None:
        """
        Decomposition tree: the reader drills `measure` down by any of the
        explain_by columns, in any order (the interactive "why?" visual).

        expand: explain_by columns opened on load, in order (e.g. ["source"]),
                so the page never opens on a lone root bar.
        Encoding ground-truthed 2026-09-26 (Analyze / ExplainBy roles;
        expansionStates levels with isPinned).
        """
        mf = self._meas(table, measure)
        expand = list(expand or [])
        for c in expand:
            if c not in explain_by:
                raise ValueError(f"expand: '{c}' is not in explain_by")
        # expanded columns lead, in expand order; the rest follow
        order = expand + [c for c in explain_by if c not in expand]
        explain = [dict(self._col(table, c), active=c in expand) for c in order]
        qs = {"Analyze": {"projections": [mf]}, "ExplainBy": {"projections": explain}}
        v = _make_visual(
            _uid(), x, y, w, h, "decompositionTreeVisual", qs,
            title=title, style=style, z=z,
            objects={"tree": [{"properties": {
                "effectiveBarsPerLevel": _lit_int(bars_per_level)}}]},
        )
        if expand:
            # EVERY explain-by column needs a level (pinned = expanded); a state
            # listing only the expanded ones crashes the visual on render
            # ("getAILevelInformation") — ground-truthed 2026-09-26.
            levels = []
            for c in order:
                t, col = _split_ref(table, c)
                lvl = {"queryRefs": [f"{t}.{col}"], "isCollapsed": True}
                if c in expand:
                    lvl["isPinned"] = True
                levels.append(lvl)
            v["visual"]["expansionStates"] = [{"roles": ["ExplainBy"],
                                               "levels": levels, "root": {}}]
        self._visuals.append(v)

    def add_insight_panel(self, title: str, table: str, measures: list,
                          x: int = 20, y: int = 160, w: int = 400, h: int = 300,
                          style: str = None, z: int = 1000) -> None:
        """
        "Key insights" panel — each line is a TEXT measure written in DAX
        (e.g. "Engineering made 69 of 223 hires (31%)"), so the sentences are
        computed, filter-aware and checkable, unlike an AI narrative or a typed
        text box. Title + one card per insight, sharing ONE card slot (same
        mechanism as the KPI tile). design_measures.add_top_insight() writes the
        most common one.
        """
        if not measures:
            raise ValueError("add_insight_panel needs at least one text measure")
        s = STYLES.get(style or DEFAULT_STYLE, STYLES[DEFAULT_STYLE])
        gid = uuid.uuid4().hex[:10]
        head = _make_visual(
            f"kt_{gid}_head", x + 8, y + 2, w - 16, 42, "textbox", None,
            style=style, z=z, background=False)
        head["visual"]["objects"] = {"general": [{"properties": {"paragraphs": [
            {"textRuns": [{"value": title, "textStyle": {
                "fontSize": "12pt", "fontFamily": "Segoe UI Semibold",
                "color": s["text_primary"]}}]}]}}]}
        self._visuals.append(head)
        n = len(measures)
        row_h = (h - 50) / n
        for i, m in enumerate(measures):
            mf = self._meas(table, m)
            v = _make_visual(
                f"kt_{gid}_i{i}", int(x + 12), int(y + 46 + i * row_h), int(w - 24),
                int(row_h - 4), "card", query_state={"Values": {"projections": [mf]}},
                style=style, z=z,
                objects={"labels": [{"properties": {
                            "fontSize": _lit_num(11), "color": _solid(s["text_primary"]),
                            "fontFamily": _lit_str("Segoe UI")}}],
                         "categoryLabels": [{"properties": {"show": _lit_bool(False)}}],
                         "wordWrap": [{"properties": {"show": _lit_bool(True)}}]},
                background=False)
            _tile_container(v, title_off=True, alt=f"{title}: insight {i + 1}")
            self._visuals.append(v)
        self._tiles[gid] = {"x": x, "y": y, "w": w, "h": h, "icon": None, "color": None}

    def add_multi_row_card(self, title: str, table: str, measures: list,
                           x: int = 20, y: int = 20, w: int = 390, h: int = 200,
                           style: str = None, z: int = 1000) -> None:
        """Multi-row card: several measures in one card."""
        projections = [self._meas(table, m) for m in measures]
        self._visuals.append(_make_visual(
            _uid(), x, y, w, h, "multiRowCard",
            query_state={"Values": {"projections": projections}},
            title=title, style=style, z=z,
        ))

    def add_gauge(self, title: str, table: str, value_measure: str,
                  min_val: float = None, max_val: float = None,
                  target_measure: str = None,
                  x: int = 20, y: int = 20, w: int = 250, h: int = 200,
                  style: str = None, z: int = 1000) -> None:
        """
        Gauge / dial chart.

        value_measure: DAX measure for the needle position (required)
        min_val:       static minimum value (optional, e.g. 0)
        max_val:       static maximum value (optional, e.g. 100)
        target_measure: DAX measure for the target line (optional)

        [!] Static min/max go into visual.objects.axis — NEVER as queryState
        projections. A Literal projection field makes Power BI reject the
        ENTIRE report ("Failed to load the report") — confirmed 2026-07-02.
        """
        qs = {"Y": {"projections": [self._meas(table, value_measure)]}}
        if target_measure:
            qs["TargetValue"] = {"projections": [self._meas(table, target_measure)]}
        objs = None
        axis_props = {}
        if min_val is not None:
            axis_props["min"] = _lit_num(min_val)
        if max_val is not None:
            axis_props["max"] = _lit_num(max_val)
        if axis_props:
            objs = {"axis": [{"properties": axis_props}]}
        self._visuals.append(_make_visual(
            _uid(), x, y, w, h, "gauge",
            query_state=qs, title=title, style=style, z=z, objects=objs,
        ))

    # ── Bar / Column family ────────────────────────────────────────────────────

    def _apply_top_n(self, v: dict, top_n, table, category_col, measure) -> None:
        """Attach a visual-level Top-N filter (requires measure= as the by-value)."""
        if not top_n:
            return
        if not measure:
            raise ValueError("top_n= requires measure= (the ranking by-value)")
        ct, cc = _split_ref(table, category_col)
        mt, mm = _split_ref(table, measure)
        v["filterConfig"] = {"filters": [_topn_filter(ct, cc, mt, mm, top_n)]}

    def add_bar_chart(self, title: str, table: str, category_col: str,
                      value_col: str = None, agg: str = "count",
                      measure: str = None, sorted_desc: bool = True,
                      series_colors: dict = None, top_n: int = None,
                      color_measure=None,
                      x: int = 20, y: int = 160, w: int = 590, h: int = 510,
                      style: str = None, z: int = 1000) -> None:
        """Horizontal clustered bar chart. Best for category ranking.
        top_n=10 keeps only the 10 largest categories (requires measure=).
        color_measure= a measure returning a hex color per bar — use
        design_measures.add_highlight_color() so the bar that answers the
        title is accented and the rest are muted (filter-aware)."""
        y_field   = self._yf(table, value_col or category_col, agg, measure)
        cat_field = self._col(table, category_col)
        qs = {"Category": {"projections": [cat_field]}, "Y": {"projections": [y_field]}}
        objs = self._series_objs(_objs_bar(), table, category_col, series_colors,
                                 color_measure)
        v = _make_visual(
            _uid(), x, y, w, h, "clusteredBarChart", qs,
            title=title, sort_field=y_field, sort_desc=sorted_desc,
            style=style, objects=objs, z=z,
        )
        self._apply_top_n(v, top_n, table, category_col, measure)
        self._visuals.append(v)

    def add_column_chart(self, title: str, table: str, category_col: str,
                         value_col: str = None, agg: str = "count",
                         measure: str = None, sorted_desc: bool = False,
                         series_colors: dict = None, top_n: int = None,
                         color_measure=None,
                         x: int = 20, y: int = 160, w: int = 590, h: int = 510,
                         style: str = None, z: int = 1000) -> None:
        """Vertical clustered column chart. Best for time series or few categories.
        color_measure= per-column hex color measure (see add_bar_chart)."""
        y_field   = self._yf(table, value_col or category_col, agg, measure)
        cat_field = self._col(table, category_col)
        qs = {"Category": {"projections": [cat_field]}, "Y": {"projections": [y_field]}}
        sort = y_field if sorted_desc else None
        objs = self._series_objs(_objs_column(), table, category_col, series_colors,
                                 color_measure)
        v = _make_visual(
            _uid(), x, y, w, h, "clusteredColumnChart", qs,
            title=title, sort_field=sort, sort_desc=sorted_desc,
            style=style, objects=objs, z=z,
        )
        self._apply_top_n(v, top_n, table, category_col, measure)
        self._visuals.append(v)

    def add_stacked_bar_chart(self, title: str, table: str, category_col: str,
                               series_col: str,
                               value_col: str = None, agg: str = "count",
                               measure: str = None,
                               series_colors: dict = None,
                               x: int = 20, y: int = 160, w: int = 590, h: int = 510,
                               style: str = None, z: int = 1000) -> None:
        """Stacked horizontal bar chart. series_col provides the color grouping."""
        y_field   = self._yf(table, value_col or category_col, agg, measure)
        cat_field = self._col(table, category_col)
        ser_field = self._col(table, series_col)
        qs = {
            "Category": {"projections": [cat_field]},
            "Series":   {"projections": [ser_field]},
            "Y":        {"projections": [y_field]},
        }
        objs = self._series_objs(_objs_bar_series(), table, series_col, series_colors)
        # Core PBI name for stacked bar is "barChart" ("stackedBarChart" does not
        # exist — PBI treats it as a missing custom visual)
        self._visuals.append(_make_visual(
            _uid(), x, y, w, h, "barChart", qs,
            title=title, style=style, objects=objs, z=z,
        ))

    def add_stacked_column_chart(self, title: str, table: str, category_col: str,
                                  series_col: str,
                                  value_col: str = None, agg: str = "count",
                                  measure: str = None,
                                  series_colors: dict = None,
                                  x: int = 20, y: int = 160, w: int = 590, h: int = 510,
                                  style: str = None, z: int = 1000) -> None:
        """Stacked vertical column chart. series_col provides the color grouping."""
        y_field   = self._yf(table, value_col or category_col, agg, measure)
        cat_field = self._col(table, category_col)
        ser_field = self._col(table, series_col)
        qs = {
            "Category": {"projections": [cat_field]},
            "Series":   {"projections": [ser_field]},
            "Y":        {"projections": [y_field]},
        }
        objs = self._series_objs(_objs_bar_series(), table, series_col, series_colors)
        # Core PBI name for stacked column is "columnChart"
        self._visuals.append(_make_visual(
            _uid(), x, y, w, h, "columnChart", qs,
            title=title, style=style, objects=objs, z=z,
        ))

    # ── Line / Area family ─────────────────────────────────────────────────────

    def add_line_chart(self, title: str, table: str, category_col: str,
                       value_col: str = None, agg: str = "count",
                       measure: str = None, legend_col: str = None,
                       x: int = 20, y: int = 160, w: int = 590, h: int = 510,
                       style: str = None, z: int = 1000) -> None:
        """Line chart. Best for trends over time. legend_col= adds one line per category."""
        y_field   = self._yf(table, value_col or category_col, agg, measure)
        cat_field = self._col(table, category_col)
        qs = {"Category": {"projections": [cat_field]}, "Y": {"projections": [y_field]}}
        if legend_col:
            qs["Series"] = {"projections": [self._col(table, legend_col)]}
        self._visuals.append(_make_visual(
            _uid(), x, y, w, h, "lineChart", qs,
            title=title, style=style, objects=_objs_line(), z=z,
        ))

    def add_area_chart(self, title: str, table: str, category_col: str,
                       value_col: str = None, agg: str = "count",
                       measure: str = None,
                       x: int = 20, y: int = 160, w: int = 590, h: int = 510,
                       style: str = None, z: int = 1000) -> None:
        """Area chart. Same as line but with filled area below the line."""
        y_field   = self._yf(table, value_col or category_col, agg, measure)
        cat_field = self._col(table, category_col)
        qs = {"Category": {"projections": [cat_field]}, "Y": {"projections": [y_field]}}
        self._visuals.append(_make_visual(
            _uid(), x, y, w, h, "areaChart", qs,
            title=title, style=style, objects=_objs_line(), z=z,
        ))

    # ── Pie / Donut / Funnel ───────────────────────────────────────────────────

    def add_pie_chart(self, title: str, table: str, category_col: str,
                      value_col: str = None, agg: str = "count",
                      measure: str = None, series_colors: dict = None,
                      x: int = 20, y: int = 160, w: int = 400, h: int = 400,
                      style: str = None, z: int = 1000) -> None:
        """Pie chart. Use only for 2-5 categories. Prefer add_donut_chart."""
        y_field   = self._yf(table, value_col or category_col, agg, measure)
        cat_field = self._col(table, category_col)
        qs = {"Category": {"projections": [cat_field]}, "Y": {"projections": [y_field]}}
        objs = self._series_objs(_objs_pie(), table, category_col, series_colors)
        self._visuals.append(_make_visual(
            _uid(), x, y, w, h, "pieChart", qs,
            title=title, sort_field=y_field, sort_desc=True,
            style=style, objects=objs, z=z,
        ))

    def add_donut_chart(self, title: str, table: str, category_col: str,
                        value_col: str = None, agg: str = "count",
                        measure: str = None, series_colors: dict = None,
                        top_n: int = None, center_measure=None,
                        center_label: str = None,
                        x: int = 20, y: int = 160, w: int = 400, h: int = 400,
                        style: str = None, z: int = 1000) -> None:
        """Donut chart. Preferred over pie -- the center area can show a total.
        center_measure= puts that measure's value in the hole ("4,300"), with
        center_label= (or the measure name) as a caption under it — a
        transparent card overlaid on the donut (no slot of its own)."""
        y_field   = self._yf(table, value_col or category_col, agg, measure)
        cat_field = self._col(table, category_col)
        qs = {"Category": {"projections": [cat_field]}, "Y": {"projections": [y_field]}}
        objs = self._series_objs(_objs_pie(), table, category_col, series_colors)
        v = _make_visual(
            _uid(), x, y, w, h, "donutChart", qs,
            title=title, sort_field=y_field, sort_desc=True,
            style=style, objects=objs, z=z,
        )
        self._apply_top_n(v, top_n, table, category_col, measure)
        self._visuals.append(v)
        if center_measure:
            s = STYLES.get(style or DEFAULT_STYLE, STYLES[DEFAULT_STYLE])
            title_h = 34 if title else 0
            cx, cy = x + w / 2, y + title_h + (h - title_h) / 2
            cw, ch = 120, 56
            label_on = True
            objs_c = {
                "labels": [{"properties": {"fontSize": _lit_num(18),
                                           "fontFamily": _lit_str("Segoe UI Semibold"),
                                           "labelDisplayUnits": _lit_num(1)}}],
                "categoryLabels": [{"properties": {"show": _lit_bool(label_on),
                                                   "fontSize": _lit_num(9),
                                                   "color": _solid(s["text_secondary"])}}],
            }
            c = _make_visual(
                "ns_" + _uid()[:17], int(cx - cw / 2), int(cy - ch / 2), cw, ch, "card",
                query_state={"Values": {"projections": [
                    dict(self._meas(table, center_measure),
                         **({"displayName": center_label} if center_label else {}))]}},
                style=style, z=z + 10, objects=objs_c, background=False)
            _tile_container(c, title_off=True, alt=f"{title}: total")
            self._visuals.append(c)

    def add_funnel_chart(self, title: str, table: str, category_col: str,
                         value_col: str = None, agg: str = "count",
                         measure: str = None,
                         x: int = 20, y: int = 160, w: int = 590, h: int = 510,
                         style: str = None, z: int = 1000) -> None:
        """
        Funnel chart. Use for sequential conversion or pipeline stages.
        Sorted by value descending — without an explicit sort PBI orders the
        stages alphabetically, which scrambles the funnel.
        """
        y_field   = self._yf(table, value_col or category_col, agg, measure)
        cat_field = self._col(table, category_col)
        qs = {"Category": {"projections": [cat_field]}, "Y": {"projections": [y_field]}}
        self._visuals.append(_make_visual(
            _uid(), x, y, w, h, "funnel", qs,
            title=title, sort_field=y_field, sort_desc=True,
            style=style, objects=_objs_funnel(), z=z,
        ))

    # ── Advanced charts ────────────────────────────────────────────────────────

    def add_treemap(self, title: str, table: str, category_col: str,
                    value_col: str = None, agg: str = "count",
                    measure: str = None, series_colors: dict = None,
                    top_n: int = None,
                    x: int = 20, y: int = 160, w: int = 590, h: int = 510,
                    style: str = None, z: int = 1000) -> None:
        """Treemap. Rectangle size represents the value. Roles: Group + Values."""
        y_field   = self._yf(table, value_col or category_col, agg, measure)
        cat_field = self._col(table, category_col)
        qs = {"Group": {"projections": [cat_field]}, "Values": {"projections": [y_field]}}
        objs = self._series_objs(_objs_treemap(), table, category_col, series_colors)
        v = _make_visual(
            _uid(), x, y, w, h, "treemap", qs,
            title=title, sort_field=y_field, sort_desc=True,
            style=style, objects=objs, z=z,
        )
        self._apply_top_n(v, top_n, table, category_col, measure)
        self._visuals.append(v)

    def add_waterfall_chart(self, title: str, table: str, category_col: str,
                             value_col: str = None, agg: str = "count",
                             measure: str = None,
                             breakdown_col: str = None,
                             x: int = 20, y: int = 160, w: int = 590, h: int = 510,
                             style: str = None, z: int = 1000) -> None:
        """
        Waterfall chart. Shows running totals with increments and decrements.
        breakdown_col: optional column for drill-down coloring.
        """
        y_field   = self._yf(table, value_col or category_col, agg, measure)
        cat_field = self._col(table, category_col)
        qs = {"Category": {"projections": [cat_field]}, "Y": {"projections": [y_field]}}
        if breakdown_col:
            qs["Breakdown"] = {"projections": [self._col(table, breakdown_col)]}
        self._visuals.append(_make_visual(
            _uid(), x, y, w, h, "waterfallChart", qs,
            title=title, style=style, objects=_objs_waterfall(), z=z,
        ))

    def add_scatter_chart(self, title: str, table: str,
                          x_col: str = None, x_measure: str = None, x_agg: str = "average",
                          y_col: str = None, y_measure: str = None, y_agg: str = "average",
                          details_col: str = None,
                          size_col: str = None,
                          legend_col: str = None,
                          series_colors: dict = None,
                          x: int = 20, y: int = 160, w: int = 590, h: int = 510,
                          style: str = None, z: int = 1000) -> None:
        """
        Scatter / bubble chart.

        x_col / x_measure : X-axis field (aggregated column or DAX measure)
        y_col / y_measure : Y-axis field (aggregated column or DAX measure)
        details_col       : column that labels each dot (entity/category)
        size_col          : optional -- makes it a bubble chart (size = SUM of col)
        legend_col        : optional column for color grouping

        Provide at least one of x_col/x_measure and one of y_col/y_measure.
        """
        if not (x_col or x_measure):
            raise ValueError("add_scatter_chart requires x_col= or x_measure=")
        if not (y_col or y_measure):
            raise ValueError("add_scatter_chart requires y_col= or y_measure=")

        xf = self._meas(table, x_measure) if x_measure else self._aggf(table, x_col, x_agg)
        yf = self._meas(table, y_measure) if y_measure else self._aggf(table, y_col, y_agg)

        qs = {"X": {"projections": [xf]}, "Y": {"projections": [yf]}}
        if details_col:
            # "Category" is the PBIR role that creates per-value dots (maps to "Values" in PBI UI)
            # "Details" is also needed as a filter reference (PBI generates both on save)
            qs["Category"] = {"projections": [self._col(table, details_col)]}
            qs["Details"]  = {"projections": [self._col(table, details_col)]}
        if size_col:
            qs["Size"] = {"projections": [self._aggf(table, size_col, "sum")]}
        if legend_col:
            qs["Legend"] = {"projections": [self._col(table, legend_col)]}

        objs = _objs_scatter()
        if series_colors and legend_col:
            objs = self._series_objs(objs, table, legend_col, series_colors)

        self._visuals.append(_make_visual(
            _uid(), x, y, w, h, "scatterChart", qs,
            title=title, style=style, objects=objs, z=z,
        ))

    def add_combo_chart(self, title: str, table: str, category_col: str,
                        bar_measure: str = None,
                        bar_value_col: str = None, bar_agg: str = "count",
                        line_measure: str = None,
                        line_value_col: str = None, line_agg: str = "average",
                        x: int = 20, y: int = 160, w: int = 590, h: int = 510,
                        style: str = None, z: int = 1000) -> None:
        """
        Line + Clustered Column combo chart (bar columns on Y, line on Y2).

        category_col  : X-axis dimension (e.g. year_month, month)
        bar_measure   : DAX measure for the column/bar series  (Y axis)
        bar_value_col : column to aggregate instead of a measure
        line_measure  : DAX measure for the line series        (Y2 axis)
        line_value_col: column to aggregate for the line series
        """
        cat_field  = self._col(table, category_col)
        bar_field  = (self._meas(table, bar_measure) if bar_measure
                      else self._aggf(table, bar_value_col or category_col, bar_agg))
        line_field = (self._meas(table, line_measure) if line_measure
                      else self._aggf(table, line_value_col or category_col, line_agg))
        qs = {
            "Category": {"projections": [cat_field]},
            "Y":        {"projections": [bar_field]},
            "Y2":       {"projections": [line_field]},
        }
        self._visuals.append(_make_visual(
            _uid(), x, y, w, h, "lineClusteredColumnComboChart", qs,
            title=title, style=style, objects=_objs_combo(), z=z,
        ))

    # ── Table / Matrix ─────────────────────────────────────────────────────────

    def _databars_objects(self, table: str, refs: list) -> dict:
        """
        columnFormatting objects turning on data bars for the given fields.
        Encoding ground-truthed 2026-07-03 (UI -> save -> read): colors are
        THEME TOKENS ('maxColor'/'minColor'/'foreground') so bars adapt to the
        embedded theme; selector is the field's queryRef metadata.
        """
        entries = []
        for r in refs:
            t, name = _split_ref(table, r)
            entries.append({
                "properties": {"dataBars": {
                    "positiveColor": _solid("maxColor"),
                    "negativeColor": _solid("minColor"),
                    "axisColor": _solid("foreground"),
                    "reverseDirection": _lit_bool(False),
                    "hideText": _lit_bool(False),
                    "totalMatchingOption": _lit_int(1),
                }},
                "selector": {"metadata": f"{t}.{name}"},
            })
        return {"columnFormatting": entries}

    def add_table(self, title: str, table: str,
                  columns: list = None,
                  measures: list = None,
                  data_bars: list = None,
                  sparklines: list = None,
                  trend_icons: dict = None,
                  x: int = 20, y: int = 160, w: int = 590, h: int = 400,
                  style: str = None, z: int = 1000) -> None:
        """
        Table visual. Columns appear left to right in the order listed.

        columns:   list of column names  (raw column values, no aggregation)
        measures:  list of DAX measure names
        data_bars: fields (from measures/columns above) that get in-cell data
                   bars — the single change that turns a plain table into an
                   analytical one. Theme-adaptive colors.
        sparklines: [{"measure": "Hires made", "date_col": "applied_month",
                     "name": "Trend", "after": "Hires made"}] — a mini trend
                     column per row (the salesperson-table look of the top
                     marketplace dashboards). "after" places it next to that
                     column; otherwise it is last — and a wide table pushes a
                     last column off-screen, so keep scorecards to ~5 columns.
        trend_icons: {"Hires made": "Hires made change"} — ▲ / ▼ / → before the
                     value, by the sign of the change measure (from
                     design_measures.add_kpi_context). Colored arrows mean
                     up = green: use only for higher-is-better measures.
        Both column lists optional; combine freely. At least one required.
        """
        projections = [self._col(table, c) for c in (columns or [])]
        projections += [self._meas(table, m) for m in (measures or [])]
        for sp in (sparklines or []):
            mt, mm = _split_ref(table, sp["measure"])
            dt, dc = _split_ref(table, sp["date_col"])
            self._meas(table, sp["measure"])
            self._col(table, sp["date_col"])
            field = _sparkline_field(mt, mm, dt, dc, sp.get("name"))
            after = sp.get("after")
            if after:
                idx = [p["nativeQueryRef"] for p in projections]
                if after not in idx:
                    raise ValueError(f"sparkline after='{after}': not a column/measure here")
                projections.insert(idx.index(after) + 1, field)
            else:
                projections.append(field)
        if not projections:
            raise ValueError("add_table requires at least one column or measure")
        objects = self._databars_objects(table, data_bars) if data_bars else {}
        if trend_icons:
            rules = []
            for col_m, chg_m in trend_icons.items():
                if col_m not in (measures or []):
                    raise ValueError(f"trend_icons: '{col_m}' is not one of measures=")
                ct, cm = _split_ref(table, col_m)
                gt, gm = _split_ref(table, chg_m)
                self._meas(table, chg_m)
                rules.append(_trend_icon_rule(ct, cm, gt, gm))
            objects["values"] = rules
        qs = {"Values": {"projections": projections}}
        self._visuals.append(_make_visual(
            _uid(), x, y, w, h, "tableEx", qs,
            title=title, style=style, z=z, objects=objects or None,
        ))

    def add_matrix(self, title: str, table: str,
                   row_cols: list = None,
                   col_cols: list = None,
                   value_measures: list = None,
                   value_cols: list = None, value_agg: str = "sum",
                   data_bars: list = None,
                   x: int = 20, y: int = 160, w: int = 590, h: int = 400,
                   style: str = None, z: int = 1000) -> None:
        """
        Matrix visual (cross-tabulation / pivot table).

        row_cols:       list of column names for the row hierarchy
        col_cols:       list of column names for the column header
        value_measures: list of DAX measure names for the values area
        value_cols:     list of column names for values (aggregated with value_agg)

        At least one value_measure or value_col is required.
        """
        rows_proj = [self._col(table, c) for c in (row_cols or [])]
        cols_proj = [self._col(table, c) for c in (col_cols or [])]
        vals_proj = [self._meas(table, m) for m in (value_measures or [])]
        vals_proj += [self._aggf(table, c, value_agg) for c in (value_cols or [])]
        if not vals_proj:
            raise ValueError("add_matrix requires at least one value_measure or value_col")
        qs = {}
        if rows_proj:
            qs["Rows"]    = {"projections": rows_proj}
        if cols_proj:
            qs["Columns"] = {"projections": cols_proj}
        qs["Values"] = {"projections": vals_proj}
        # "pivotTable" is the MODERN matrix core visual (stepped layout,
        # +/- drill icons). "matrix" is the legacy visual: it renders every
        # row field as a flat column and has no hierarchy support at all
        # (render-verified 2026-07-18).
        self._visuals.append(_make_visual(
            _uid(), x, y, w, h, "pivotTable", qs,
            title=title, style=style, z=z,
            objects=self._databars_objects(table, data_bars) if data_bars else None,
        ))

    # ── Slicer / Text box ──────────────────────────────────────────────────────

    def add_slicer(self, table: str, column: str,
                   slicer_type: str = "dropdown", title: str = None,
                   show_header: bool = True, single_select: bool = False,
                   x: int = 20, y: int = 20, w: int = 200, h: int = 60,
                   style: str = None, z: int = 1000) -> None:
        """
        Slicer for interactive filtering.

        slicer_type: "dropdown" (compact, default), "list" (all values visible),
                     "between" (numeric/date range slider — use for date columns),
                     or "tiles" (one pill per value in a row, e.g. 2024 | 2025 |
                     2026 — the header-control look of top marketplace dashboards).
        show_header=False hides the column-name header (right for tiles in a
        header band); title= adds a card title on top.
        """
        modes = {"dropdown": "Dropdown", "list": "Basic", "between": "Between",
                 "tiles": "Basic"}
        if slicer_type not in modes:
            raise ValueError(f"slicer_type must be one of {list(modes)}")
        qs = {"Values": {"projections": [self._col(table, column)]}}
        objects = {
            "data":   [{"properties": {"mode": _lit_str(modes[slicer_type])}}],
            "header": [{"properties": {"show": _lit_bool(show_header),
                                       "fontSize": _lit_num(10)}}],
        }
        if slicer_type == "tiles":
            # ground-truthed 2026-09-26 (Format > Slicer settings > Style: Tile)
            objects["general"] = [{"properties": {"orientation": _lit_num(1)}}]
        if single_select:
            objects["selection"] = [{"properties": {"singleSelect": _lit_bool(True)}}]
        self._visuals.append(_make_visual(
            _uid(), x, y, w, h, "slicer", qs,
            title=title, style=style, objects=objects, z=z,
        ))

    def add_text_box(self, text: str,
                     x: int = 20, y: int = 20, w: int = 600, h: int = 50,
                     font_size: int = 18, bold: bool = True,
                     color: str = None, align: str = "left",
                     transparent: bool = True,
                     style: str = None, z: int = 100) -> None:
        """
        Static text box — use for dashboard headers / section labels.

        text:        the text to display (use \\n for multiple paragraphs)
        font_size:   point size (default 18)
        color:       hex color; defaults to the style's text_primary
        align:       "left" | "center" | "right"
        transparent: no card background (default True — sits on the page background)
        z:           defaults to 100 so headers sit behind charts if overlapped
        """
        s = STYLES.get(style or DEFAULT_STYLE, STYLES[DEFAULT_STYLE])
        run_style = {"fontSize": f"{font_size}pt"}
        if bold:
            run_style["fontWeight"] = "bold"
        run_style["color"] = color or s["text_primary"]

        paragraphs = []
        for line in str(text).split("\n"):
            p = {"textRuns": [{"value": line, "textStyle": dict(run_style)}]}
            if align != "left":
                p["horizontalTextAlignment"] = align
            paragraphs.append(p)

        v = _make_visual(
            _uid(), x, y, w, h, "textbox", query_state=None,
            title=None, style=style, z=z, background=not transparent,
        )
        v["visual"]["objects"] = {"general": [{"properties": {"paragraphs": paragraphs}}]}
        self._visuals.append(v)

    # ── Design-layer visuals (Phase 2) ─────────────────────────────────────────
    # Encodings ground-truthed against PBI-saved visual.json files (2026-07-02):
    # shape.tileShape / fill.fillColor+selector, image.sourceFile.image,
    # bare pageNavigator. All are NO_SLOT_TYPES (no background card slot drawn).

    def add_shape(self, shape: str = "rectangleRounded",
                  fill: str = None, transparency: int = 0,
                  rotation: int = 0,
                  x: int = 20, y: int = 20, w: int = 200, h: int = 100,
                  style: str = None, z: int = 900) -> None:
        """
        Decorative shape — header bands, dividers, accent bars, rail overlays.

        shape: "rectangle" | "rectangleRounded" | "oval" | "triangle" | "line"
               (PBI tileShape names; rectangleRounded verified)
        fill:  hex color; defaults to the style's accent
        z:     default 900 — behind data visuals (z 1000+), above the background
        """
        s = STYLES.get(style or DEFAULT_STYLE, STYLES[DEFAULT_STYLE])
        v = _make_visual(
            _uid(), x, y, w, h, "shape", query_state=None,
            title=None, style=style, z=z, background=False,
        )
        v["visual"]["objects"] = {
            "shape":    [{"properties": {"tileShape": _lit_str(shape)}}],
            "rotation": [{"properties": {"shapeAngle": _lit_int(rotation)}}],
            "fill":     [{
                "properties": {
                    "fillColor": _solid(fill or s["accent"]),
                    "transparency": _lit_num(transparency),
                },
                "selector": {"id": "default"},
            }],
            # shapes default to a dark outline that swallows thin accent bars
            "outline":  [{"properties": {"show": _lit_bool(False)}}],
        }
        self._visuals.append(v)

    def add_image(self, image_path: str, title: str = None,
                  scaling: str = "Normal",
                  x: int = 20, y: int = 20, w: int = 100, h: int = 100,
                  z: int = 1100, force: bool = False) -> None:
        """
        Image visual (logo, icon, illustration). Copies the file into
        RegisteredResources and registers it in report.json.

        scaling: "Normal" (verified) | "Fit" | "Fill"
        """
        if self._editor is None:
            raise RuntimeError("add_image() requires a page obtained from a PbirEditor.")
        if not force and self._editor.guard_pbi and powerbi_is_open():
            raise RuntimeError(
                "Power BI Desktop is running — close it before writing to .Report/. "
                "Use add_image(..., force=True) to override."
            )
        fname = self._editor._register_image(image_path)
        v = _make_visual(
            _uid(), x, y, w, h, "image", query_state=None,
            title=None, style=None, z=z, background=False,
        )
        v["visual"]["objects"] = {
            "image": [{"properties": {"sourceFile": {"image": {
                "name": _lit_str(fname),
                "url": {"expr": {"ResourcePackageItem": {
                    "PackageName": "RegisteredResources",
                    "PackageType": 1,
                    "ItemName": fname,
                }}},
                "scaling": _lit_str(scaling),
            }}}}],
        }
        self._visuals.append(v)

    def add_icon(self, glyph: str, color: str = None, size: int = 32,
                 x: int = 20, y: int = 20,
                 style: str = None, z: int = 1100, force: bool = False) -> None:
        """
        Small icon rendered from the Segoe MDL2 Assets glyph font (built into
        Windows) — no external assets. See icon_generator.GLYPHS for names.

        glyph: a name from icon_generator.GLYPHS ("people", "cart", ...) or a
               literal glyph character (e.g. "\\uE716")
        color: hex; defaults to the style's accent
        """
        from icon_generator import generate_icon  # lazy Pillow import
        if self._editor is None:
            raise RuntimeError("add_icon() requires a page obtained from a PbirEditor.")
        # guard BEFORE the PNG lands in .Report/ (add_image guards report.json,
        # but the icon file itself is written first)
        if not force and self._editor.guard_pbi and powerbi_is_open():
            raise RuntimeError(
                "Power BI Desktop is running — close it before writing to .Report/. "
                "Use add_icon(..., force=True) to override."
            )
        s = STYLES.get(style or DEFAULT_STYLE, STYLES[DEFAULT_STYLE])
        res_dir = os.path.join(self._editor.report_folder,
                               "StaticResources", "RegisteredResources")
        os.makedirs(res_dir, exist_ok=True)
        png = generate_icon(glyph, color or s["accent"], out_dir=res_dir, size=size)
        self.add_image(png, scaling="Fit", x=x, y=y, w=size, h=size, z=z, force=force)

    def add_page_navigator(self, x: int = 0, y: int = 0,
                           w: int = 200, h: int = 40, z: int = 1200,
                           orientation: str = "horizontal", text_indent: int = 0,
                           font_size: int = 11, style: str = None) -> None:
        """
        Page navigator — auto-generated page buttons that stay in sync with the
        pages and mark the current one. Styled from the style preset: muted
        text, no borders, the current page tinted in the accent with an accent
        bar (the "you are here" state every top-rated dashboard shows).

        orientation: "horizontal" (tabs, e.g. in a header) | "vertical" (menu)
        text_indent: left text margin in px — room for icons baked into the
            background (app_shell uses 40 with the icon at +14px)
        Encodings ground-truthed 2026-09-26: layout.orientation 1 = vertical;
        per-state selectors {"id": "default" | "hover" | "selected"}.
        """
        if orientation not in ("horizontal", "vertical"):
            raise ValueError("orientation must be 'horizontal' or 'vertical'")
        s = STYLES.get(style or DEFAULT_STYLE, STYLES[DEFAULT_STYLE])
        vertical = orientation == "vertical"

        def st(state, **props):
            return {"properties": props, "selector": {"id": state}}

        default_text = {"fontColor": _solid(s["text_secondary"]),
                        "fontSize": _lit_num(font_size),
                        "horizontalAlignment": _lit_str("left" if vertical else "center")}
        if text_indent:
            default_text["leftMargin"] = _lit_int(text_indent)
        objects = {
            "text": [st("default", **default_text),
                     st("hover", fontColor=_solid(s["text_primary"])),
                     st("selected", fontColor=_solid(s["text_primary"]),
                        bold=_lit_bool(True))],
            "fill": [{"properties": {"show": _lit_bool(True)}},
                     st("default", fillColor=_solid(s["card_bg"]),
                        transparency=_lit_num(100)),
                     st("hover", fillColor=_solid(s["accent"]), transparency=_lit_num(88)),
                     st("selected", fillColor=_solid(s["accent"]),
                        transparency=_lit_num(78))],
            "accentBar": [st("selected", show=_lit_bool(True), color=_solid(s["accent"]),
                             width=_lit_num(4),
                             position=_lit_str("Left" if vertical else "Bottom"))],
            "outline": [{"properties": {"show": _lit_bool(False)}}],
        }
        if vertical:
            objects["layout"] = [{"properties": {"orientation": _lit_num(1)}}]
        v = _make_visual(
            _uid(), x, y, w, h, "pageNavigator", query_state=None,
            title=None, style=None, z=z, background=False, objects=objects,
        )
        self._visuals.append(v)

    def add_clear_button(self, text: str = "Clear filters",
                         x: int = 0, y: int = 0, w: int = 120, h: int = 32,
                         z: int = 1200, style: str = None) -> None:
        """
        Button that resets every slicer on the page (Power BI action
        "Clear all slicers"). Encoding ground-truthed 2026-09-26:
        visualContainerObjects.visualLink type 'ClearAllSlicers'.
        """
        s = STYLES.get(style or DEFAULT_STYLE, STYLES[DEFAULT_STYLE])

        def st(state, **props):
            return {"properties": props, "selector": {"id": state}}

        objects = {
            "text": [{"properties": {"show": _lit_bool(True)}},
                     st("default", text=_lit_str(text),
                        fontColor=_solid(s["text_primary"]), fontSize=_lit_num(10)),
                     st("hover", fontColor=_solid(s["text_primary"]))],
            "fill": [{"properties": {"show": _lit_bool(True)}},
                     st("default", fillColor=_solid(s["card_bg"]), transparency=_lit_num(0)),
                     st("hover", fillColor=_solid(s["accent"]), transparency=_lit_num(80))],
            "outline": [{"properties": {"show": _lit_bool(True)}},
                        st("default", lineColor=_solid(s["border"]))],
            "icon": [{"properties": {"show": _lit_bool(False)}}],
        }
        v = _make_visual("ns_" + _uid()[:17], x, y, w, h, "actionButton", None,
                         style=style, z=z, background=False, objects=objects)
        vco = v["visual"]["visualContainerObjects"]
        vco["visualLink"] = [{"properties": {
            "show": _lit_bool(True), "type": _lit_str("ClearAllSlicers")}}]
        vco["general"] = [{"properties": {"altText": _lit_str(f"Button: {text}")}}]
        self._visuals.append(v)

    def add_text_measure(self, table: str, measure: str,
                         x: int = 0, y: int = 0, w: int = 200, h: int = 24,
                         font_size: int = 9, color: str = None,
                         style: str = None, z: int = 1100) -> None:
        """
        One line of text from a TEXT measure — e.g. "Data through 18 Sep 2026"
        (design_measures.add_data_through) in a menu footer. Unlike a text box
        it stays true when the data refreshes. No title, label or card slot.
        """
        s = STYLES.get(style or DEFAULT_STYLE, STYLES[DEFAULT_STYLE])
        mf = self._meas(table, measure)
        v = _make_visual(
            "ns_" + _uid()[:17], x, y, w, h, "card",
            query_state={"Values": {"projections": [mf]}}, style=style, z=z,
            objects={"labels": [{"properties": {
                        "fontSize": _lit_num(font_size),
                        "color": _solid(color or s["text_secondary"]),
                        "fontFamily": _lit_str("Segoe UI")}}],
                     "categoryLabels": [{"properties": {"show": _lit_bool(False)}}]},
            background=False)
        _tile_container(v, title_off=True, alt=f"Text: {measure}")
        self._visuals.append(v)

    # ── Utilities ──────────────────────────────────────────────────────────────

    def list_visuals(self) -> list:
        """[(name, visualType, title), ...] for everything queued (incl. loaded from disk)."""
        return [(v.get("name"), v.get("visual", {}).get("visualType"), _visual_title(v))
                for v in self._visuals]

    def remove_visual(self, key: str) -> bool:
        """Remove one visual by its name (folder id) or its title. True if removed."""
        for i, v in enumerate(self._visuals):
            if v.get("name") == key or _visual_title(v) == key:
                del self._visuals[i]
                return True
        return False

    def set_alt_text(self, key: str, text: str) -> bool:
        """
        Override the auto-generated alt text on a queued visual (by title or
        name). Auto text is "<Friendly type>: <title>"; use this when the
        visual carries an insight worth narrating for screen readers.
        """
        for v in self._visuals:
            if v.get("name") == key or _visual_title(v) == key:
                vco = v["visual"].setdefault("visualContainerObjects", {})
                vco["general"] = [{"properties": {"altText": _lit_str(text)}}]
                return True
        return False

    def clear_visuals(self) -> None:
        """
        Remove ALL visuals — including existing ones loaded from disk.
        The deletion hits disk on the next .save(). Use remove_visual() to
        delete selectively.
        """
        self._visuals.clear()
        self._tiles.clear()

    @staticmethod
    def _tile_gid(v: dict):
        """KPI-tile group id encoded in the visual name (kt_<gid>_<part>),
        or None for ordinary visuals."""
        name = v.get("name", "")
        if name.startswith("kt_") and name.count("_") >= 2:
            return name.split("_")[1]
        return None

    def _tile_rects(self) -> dict:
        """gid -> {"x","y","w","h","icon","color"} for every KPI tile on the
        page. Tiles added this session carry their exact slot + icon; tiles
        loaded from disk fall back to the union of their parts (no icon)."""
        rects = {g: dict(t) for g, t in self._tiles.items()}
        parts = {}
        for v in self._visuals:
            g = self._tile_gid(v)
            if g and g not in rects:
                parts.setdefault(g, []).append(v["position"])
        for g, ps in parts.items():
            x0 = min(p["x"] for p in ps) - 8
            y0 = min(p["y"] for p in ps) - 4
            x1 = max(p["x"] + p["width"] for p in ps) + 8
            y1 = max(p["y"] + p["height"] for p in ps) + 6
            rects[g] = {"x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0,
                        "icon": None, "color": None}
        return rects

    def find_overlaps(self) -> list:
        """
        Pairwise intersection check over data visuals (textboxes/shapes/images
        excluded — they layer by design). Returns [(title_a, title_b), ...].
        """
        rects = []
        for v in self._visuals:
            if _no_slot(v):
                continue
            p = v["position"]
            rects.append((_visual_title(v) or v["name"],
                          p["x"], p["y"], p["x"] + p["width"], p["y"] + p["height"]))
        hits = []
        for i in range(len(rects)):
            for j in range(i + 1, len(rects)):
                a, b = rects[i], rects[j]
                if a[1] < b[3] and b[1] < a[3] and a[2] < b[4] and b[2] < a[4]:
                    hits.append((a[0], b[0]))
        return hits

    def set_canvas_size(self, width: int = None, height: int = None) -> None:
        """Set page canvas dimensions. Call before save() for multi-row dashboards."""
        if width:
            self.width = width
        if height:
            self.height = height

    # ── Page background (competition-style) ────────────────────────────────────

    def set_background(self, image_path: str, transparency: int = 0,
                       scaling: str = "Fit", force: bool = False) -> None:
        """
        Set a page background image. Copies the image into
        StaticResources/RegisteredResources, registers it in report.json,
        and wires page.json objects.background. Persisted on save().
        """
        if self._editor is None:
            raise RuntimeError("set_background() requires a page obtained from a PbirEditor.")
        if not force and self._editor.guard_pbi and powerbi_is_open():
            raise RuntimeError(
                "Power BI Desktop is running — close it before writing to .Report/. "
                "Use set_background(..., force=True) to override."
            )
        fname = self._editor._register_image(image_path)
        self._page_json.setdefault("objects", {})["background"] = [{
            "properties": {
                "image": {"image": {
                    "name": _lit_str(fname),
                    "url": {"expr": {"ResourcePackageItem": {
                        "PackageName": "RegisteredResources",
                        "PackageType": 1,
                        "ItemName": fname,
                    }}},
                    "scaling": _lit_str(scaling),
                }},
                "transparency": _lit_num(transparency),
            }
        }]
        print(f"  [{self.display_name}] page background -> {fname}")

    def make_visuals_transparent(self) -> None:
        """Turn off the card background on every queued visual (they sit on
        the background image's pre-drawn slots instead)."""
        for v in self._visuals:
            vco = v["visual"].setdefault("visualContainerObjects", {})
            vco["background"] = [{"properties": {"show": _lit_bool(False)}}]

    def generate_background(self, header_title: str = None,
                            header_subtitle: str = None,
                            header_height: int = 90,
                            style: str = None, nav_rail=0,
                            shadow: bool = None, radius: int = 10,
                            out_name: str = None, force: bool = False,
                            header_reserve_right: int = 0) -> None:
        """
        Competition-style page background: generates a PNG with a header band,
        optional nav rail, and a pre-drawn card slot (rounded corners + soft
        shadow) under every queued visual — then wires it as the page
        background and makes all visuals transparent.

        Call AFTER all add_* calls, right before save().

        header_title/header_subtitle: baked into the header band (Segoe UI).
                    Omit both to skip the band entirely.
        nav_rail:   left rail width in px (0 = none; 56 is typical), or a
                    dict for a labeled app menu — {"width", "brand", "tagline",
                    "logo_glyph", "items": [{"glyph", "y", "h", "active"}],
                    "icon_x"} (see bg_generator; layout_templates.app_shell)
        shadow:     default ON except for minimal_neutral (which uses borders)
        Visuals that would overlap the header band are shifted down once.
        """
        from bg_generator import generate_background as _draw  # lazy Pillow import

        if not self._visuals:
            raise RuntimeError(
                "Add visuals first — generate_background() draws card slots "
                "from their positions."
            )
        if self._editor is None:
            raise RuntimeError("generate_background() requires a page obtained from a PbirEditor.")
        if not force and self._editor.guard_pbi and powerbi_is_open():
            raise RuntimeError(
                "Power BI Desktop is running — close it before writing to .Report/. "
                "Use generate_background(..., force=True) to override."
            )

        style_key = style or DEFAULT_STYLE
        s = STYLES.get(style_key, STYLES[DEFAULT_STYLE])
        if shadow is None:
            shadow = style_key != "minimal_neutral"
        border = style_key == "minimal_neutral"

        header_on = bool(header_title or header_subtitle)
        band_h = header_height if header_on else 0

        # Shift visuals below the header band (only if any SLOT visual overlaps —
        # idempotent; design-layer visuals like page navigators and rail icons
        # legitimately live inside the header/rail zone and are ignored)
        if header_on:
            slot_ys = [v["position"]["y"] for v in self._visuals if not _no_slot(v)]
            if slot_ys and min(slot_ys) < band_h + 10:
                delta = band_h + 20 - min(slot_ys)
                for v in self._visuals:
                    if not _no_slot(v):
                        v["position"]["y"] += delta
                for t in self._tiles.values():
                    t["y"] += delta
                print(f"  [{self.display_name}] visuals shifted down {delta}px to clear the header")

        # Final canvas size — same math as save()'s auto-grow, done up front so
        # the image dimensions always match the canvas exactly.
        max_bottom = max(v["position"]["y"] + v["position"]["height"] for v in self._visuals)
        max_right  = max(v["position"]["x"] + v["position"]["width"]  for v in self._visuals)
        self.height = max(self.height, max_bottom + 20)
        self.width  = max(self.width,  max_right + 20)

        slots = []
        for v in self._visuals:
            vt = v["visual"]["visualType"]
            if _no_slot(v) or self._tile_gid(v):
                continue
            p = v["position"]
            slots.append({"x": p["x"], "y": p["y"], "w": p["width"], "h": p["height"],
                          "accent": "left" if vt in KPI_TYPES else None})
        # one slot per KPI tile (its parts share it), with the icon tile drawn in
        for t in self._tile_rects().values():
            slot = {"x": t["x"], "y": t["y"], "w": t["w"], "h": t["h"], "accent": None}
            if t.get("icon"):
                slot["icon"] = {"glyph": t["icon"], "color": t["color"]}
            slots.append(slot)

        fname = out_name or f"bg_{self.page_id}.png"
        res_dir = os.path.join(self._editor.report_folder,
                               "StaticResources", "RegisteredResources")
        os.makedirs(res_dir, exist_ok=True)
        img_path = os.path.join(res_dir, fname)

        _draw(img_path, self.width, self.height, colors=s, slots=slots,
              header={"title": header_title, "subtitle": header_subtitle,
                      "height": band_h, "reserve_right": header_reserve_right}
                     if header_on else None,
              nav_rail=(nav_rail if isinstance(nav_rail, dict)
                        else {"width": nav_rail}) if nav_rail else None,
              radius=radius, shadow=shadow, border=border)

        self.set_background(img_path, force=force)
        self.make_visuals_transparent()
        print(f"  [{self.display_name}] competition background generated "
              f"({self.width}x{self.height}, {len(slots)} slots)")

    def save(self, force: bool = False) -> None:
        """
        Write page.json and all visual.json files to disk.

        Raises if Power BI Desktop is running (it would ignore/overwrite the
        files) — pass force=True only if you are sure the project is not open.
        Auto-grows the canvas if any visual extends beyond it.
        """
        if not force and (self._editor is None or self._editor.guard_pbi) and powerbi_is_open():
            raise RuntimeError(
                "Power BI Desktop is running — close it before writing to .Report/ "
                "(open PBI ignores/overwrites these files on next save). "
                "If the project is open in PBI you WILL lose this write. "
                "Use save(force=True) to override."
            )

        # Auto-grow canvas to fit content
        if self._visuals:
            max_bottom = max(v["position"]["y"] + v["position"]["height"] for v in self._visuals)
            max_right  = max(v["position"]["x"] + v["position"]["width"]  for v in self._visuals)
            if max_bottom + 20 > self.height:
                print(f"  [{self.display_name}] canvas height {self.height} -> {max_bottom + 20} (auto-grow)")
                self.height = max_bottom + 20
            if max_right + 20 > self.width:
                print(f"  [{self.display_name}] canvas width {self.width} -> {max_right + 20} (auto-grow)")
                self.width = max_right + 20

        # Overlap check — data visuals must never overlap (design rule; also
        # makes generate_background() draw overlapping card slots)
        for a, b in self.find_overlaps():
            print(f"  [{self.display_name}] [!] OVERLAP: '{a}' intersects '{b}' — fix x/y/w/h")

        visuals_dir = os.path.join(self.page_dir, "visuals")
        if os.path.exists(visuals_dir):
            shutil.rmtree(visuals_dir)
        os.makedirs(visuals_dir, exist_ok=True)

        page_json = dict(self._page_json)
        page_json["$schema"]       = page_json.get("$schema", SCHEMA_PAGE)
        page_json["name"]          = self.page_id
        page_json["displayName"]   = self.display_name
        page_json["displayOption"] = page_json.get("displayOption", "FitToPage")
        page_json["height"]        = self.height
        page_json["width"]         = self.width
        with open(os.path.join(self.page_dir, "page.json"), "w", encoding="utf-8") as f:
            json.dump(page_json, f, indent=2, ensure_ascii=False)

        for v in self._visuals:
            vdir = os.path.join(visuals_dir, v["name"])
            os.makedirs(vdir, exist_ok=True)
            with open(os.path.join(vdir, "visual.json"), "w", encoding="utf-8") as f:
                json.dump(v, f, indent=2, ensure_ascii=False)

        print(f"  [{self.display_name}] Wrote {len(self._visuals)} visuals -> {self.page_dir}")


# ── Editor class ───────────────────────────────────────────────────────────────

class PbirEditor:
    """
    Entry point for editing a PBIR report.

    Usage:
        ed = PbirEditor(r"C:\\...\\project.Report")
        page = ed.get_page(0)   # first page by index, or get_page("Page 1") by name
        page.add_card_measure(...)
        page.save()
        print(ed.summary())

    validate=True (default): field names are checked against the sibling
    .SemanticModel TMDL files; typos raise immediately with suggestions.
    guard_pbi=True (default): save() refuses to write while PBIDesktop.exe runs.
    """

    def __init__(self, report_folder: str, validate: bool = True,
                 guard_pbi: bool = True, semantic_model_folder: str = None):
        self.report_folder = report_folder
        self.def_folder    = os.path.join(report_folder, "definition")
        self.pages_folder  = os.path.join(self.def_folder, "pages")
        self.guard_pbi     = guard_pbi

        if semantic_model_folder is None:
            base = report_folder[:-len(".Report")] if report_folder.rstrip("\\/").endswith(".Report") \
                   else report_folder.rstrip("\\/")
            semantic_model_folder = base + ".SemanticModel"
        self.semantic_model_folder = semantic_model_folder

        self.model_index = None
        self.validate    = False
        if validate and os.path.isdir(os.path.join(semantic_model_folder, "definition", "tables")):
            self.model_index = ModelIndex(semantic_model_folder)
            self.validate    = bool(self.model_index.tables)

    # ── page management ────────────────────────────────────────────────────────

    def _page_folders(self) -> list:
        if not os.path.exists(self.pages_folder):
            raise FileNotFoundError(
                f"Pages folder not found: {self.pages_folder}\n"
                "Save the report as a Power BI Project (.pbip) in Power BI Desktop first, "
                "or scaffold one with pbip_scaffold.new_project()."
            )
        return sorted(
            d for d in os.listdir(self.pages_folder)
            if os.path.isdir(os.path.join(self.pages_folder, d))
        )

    def _read_page_json(self, folder: str) -> dict:
        path = os.path.join(self.pages_folder, folder, "page.json")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        return {"name": folder, "displayName": folder}

    def _read_pages_meta(self) -> dict:
        path = os.path.join(self.pages_folder, "pages.json")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        return {"$schema": SCHEMA_PAGES, "pageOrder": [], "activePageName": ""}

    def _write_pages_meta(self, meta: dict) -> None:
        meta["$schema"] = meta.get("$schema", SCHEMA_PAGES)
        with open(os.path.join(self.pages_folder, "pages.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2, ensure_ascii=False)

    def get_page(self, key) -> PbirPage:
        """
        Get a page by integer index (0 = first in pageOrder) or by displayName.
        Existing visuals are loaded — save() preserves them unless cleared/removed.
        """
        folders = self._page_folders()
        if not folders:
            raise FileNotFoundError("No page folders found inside the Report definition.")

        order = self._read_pages_meta().get("pageOrder", [])
        ordered = [p for p in order if p in folders] + [f for f in folders if f not in order]

        if isinstance(key, int):
            if key >= len(ordered):
                raise IndexError(f"Page index {key} out of range ({len(ordered)} pages found).")
            folder = ordered[key]
        else:
            folder = next(
                (f for f in ordered
                 if self._read_page_json(f).get("displayName") == key
                 or self._read_page_json(f).get("name") == key),
                None,
            )
            if folder is None:
                names = [self._read_page_json(f).get("displayName") for f in ordered]
                raise KeyError(f"Page '{key}' not found. Available: {names}")

        pj = self._read_page_json(folder)
        return PbirPage(
            page_dir     = os.path.join(self.pages_folder, folder),
            page_id      = pj.get("name", folder),
            display_name = pj.get("displayName", folder),
            width        = pj.get("width", 1280),
            height       = pj.get("height", 720),
            page_json    = pj,
            editor       = self,
        )

    def add_page(self, display_name: str, width: int = 1280, height: int = 720) -> PbirPage:
        """Create a new page, register it in pages.json, and return it."""
        page_id  = _uid()
        page_dir = os.path.join(self.pages_folder, page_id)
        os.makedirs(page_dir, exist_ok=True)
        pj = {
            "$schema": SCHEMA_PAGE,
            "name": page_id,
            "displayName": display_name,
            "displayOption": "FitToPage",
            "height": height,
            "width": width,
        }
        with open(os.path.join(page_dir, "page.json"), "w", encoding="utf-8") as f:
            json.dump(pj, f, indent=2, ensure_ascii=False)

        meta = self._read_pages_meta()
        meta.setdefault("pageOrder", []).append(page_id)
        if not meta.get("activePageName"):
            meta["activePageName"] = page_id
        self._write_pages_meta(meta)

        return PbirPage(page_dir, page_id, display_name, width, height,
                        page_json=pj, editor=self)

    def remove_page(self, key) -> None:
        """Delete a page (by index or displayName) and unregister it."""
        page = self.get_page(key)
        shutil.rmtree(page.page_dir)
        meta = self._read_pages_meta()
        meta["pageOrder"] = [p for p in meta.get("pageOrder", []) if p != page.page_id]
        if meta.get("activePageName") == page.page_id:
            meta["activePageName"] = meta["pageOrder"][0] if meta["pageOrder"] else ""
        self._write_pages_meta(meta)
        print(f"  Removed page '{page.display_name}'")

    # ── resource registration ──────────────────────────────────────────────────

    def _register_image(self, image_path: str) -> str:
        """
        Copy an image into StaticResources/RegisteredResources and register it
        in report.json resourcePackages (type "Image"). Returns the item name.
        Re-registering the same name replaces the existing entry.
        """
        fname = os.path.basename(image_path)
        res_dir = os.path.join(self.report_folder, "StaticResources", "RegisteredResources")
        os.makedirs(res_dir, exist_ok=True)
        dst = os.path.join(res_dir, fname)
        if os.path.abspath(image_path) != os.path.abspath(dst):
            shutil.copyfile(image_path, dst)

        report_json_path = os.path.join(self.def_folder, "report.json")
        with open(report_json_path, encoding="utf-8") as f:
            report = json.load(f)
        packages = report.setdefault("resourcePackages", [])
        reg = next((p for p in packages if p.get("type") == "RegisteredResources"), None)
        if reg is None:
            reg = {"name": "RegisteredResources", "type": "RegisteredResources", "items": []}
            packages.insert(0, reg)
        items = reg.setdefault("items", [])
        items[:] = [i for i in items if i.get("name") != fname]
        items.append({"name": fname, "path": fname, "type": "Image"})
        with open(report_json_path, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, ensure_ascii=False)
        return fname

    # ── theme embedding ────────────────────────────────────────────────────────

    def embed_theme(self, style: str, force: bool = False) -> None:
        """
        Register a theme inside the report so it applies automatically on open —
        replaces the manual View -> Themes -> Browse step.

        style: "corporate_light" | "dark_analytics" | "minimal_neutral",
               or a full path to any theme JSON file.
        """
        if self.guard_pbi and not force and powerbi_is_open():
            raise RuntimeError(
                "Power BI Desktop is running — close it before writing to .Report/. "
                "Use embed_theme(..., force=True) to override."
            )
        src = style if os.path.isfile(style) else os.path.join(
            THEMES_DIR, f"professional_{style}.json")
        if not os.path.isfile(src):
            raise FileNotFoundError(f"Theme file not found: {src}")
        fname = os.path.basename(src)

        res_dir = os.path.join(self.report_folder, "StaticResources", "RegisteredResources")
        os.makedirs(res_dir, exist_ok=True)
        shutil.copyfile(src, os.path.join(res_dir, fname))

        report_json_path = os.path.join(self.def_folder, "report.json")
        with open(report_json_path, encoding="utf-8") as f:
            report = json.load(f)

        packages = report.setdefault("resourcePackages", [])
        reg = next((p for p in packages if p.get("type") == "RegisteredResources"), None)
        if reg is None:
            reg = {"name": "RegisteredResources", "type": "RegisteredResources", "items": []}
            packages.insert(0, reg)
        reg["items"] = [i for i in reg.get("items", []) if i.get("type") != "CustomTheme"]
        reg["items"].append({"name": fname, "path": fname, "type": "CustomTheme"})

        # reportVersionAtImport is REQUIRED by the report schema — omitting it
        # makes Power BI reject the whole report ("Failed to load the report")
        theme_coll = report.setdefault("themeCollection", {})
        rvi = (theme_coll.get("baseTheme") or {}).get("reportVersionAtImport") or {
            "visual": "2.9.0", "report": "3.3.0", "page": "2.3.1",
        }
        theme_coll["customTheme"] = {
            "name": fname,
            "reportVersionAtImport": rvi,
            "type": "RegisteredResources",
        }

        with open(report_json_path, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, ensure_ascii=False)
        print(f"  Embedded theme '{fname}' (applies automatically on open)")

    # ── declarative spec builds ────────────────────────────────────────────────

    _SPEC_TYPES = {
        "card_measure": "add_card_measure", "kpi_card": "add_card_measure",
        "kpi_tile": "add_kpi_tile",
        "card": "add_card_measure",
        "multi_row_card": "add_multi_row_card",
        "gauge": "add_gauge",
        "bar_chart": "add_bar_chart", "bar": "add_bar_chart",
        "column_chart": "add_column_chart", "column": "add_column_chart",
        "stacked_bar_chart": "add_stacked_bar_chart",
        "stacked_column_chart": "add_stacked_column_chart",
        "line_chart": "add_line_chart", "line": "add_line_chart",
        "area_chart": "add_area_chart", "area": "add_area_chart",
        "pie_chart": "add_pie_chart", "pie": "add_pie_chart",
        "donut_chart": "add_donut_chart", "donut": "add_donut_chart",
        "funnel_chart": "add_funnel_chart", "funnel": "add_funnel_chart",
        "treemap": "add_treemap",
        "decomposition_tree": "add_decomposition_tree", "decomp_tree": "add_decomposition_tree",
        "insight_panel": "add_insight_panel", "insights": "add_insight_panel",
        "waterfall_chart": "add_waterfall_chart", "waterfall": "add_waterfall_chart",
        "scatter_chart": "add_scatter_chart", "scatter": "add_scatter_chart",
        "combo_chart": "add_combo_chart", "combo": "add_combo_chart",
        "table": "add_table",
        "matrix": "add_matrix",
        "slicer": "add_slicer",
        "text_box": "add_text_box", "textbox": "add_text_box",
        "shape": "add_shape",
        "image": "add_image",
        "icon": "add_icon",
        "page_navigator": "add_page_navigator", "nav": "add_page_navigator",
        "clear_button": "add_clear_button",
        "text_measure": "add_text_measure",
        "svg_card": "add_svg_card", "svg": "add_svg_card",
    }

    def build_from_spec(self, spec: dict, force: bool = False) -> str:
        """
        Build dashboard pages from a declarative spec — the Workflow R plan as data.

        spec = {
          "style": "dark_analytics",           # default style for every visual
          "theme": true,                        # embed the matching theme JSON
          "pages": [
            {
              "page": 0,                        # index or displayName ("new:<Name>" creates one)
              "canvas": {"width": 1280, "height": 1380},   # optional
              "clear": true,                    # default true: spec IS the full page state
              "background": {"title": "Sales Performance",      # optional: competition
                             "subtitle": "FY26", "nav_rail": 56}, # background (or true)
              "visuals": [
                 {"type": "kpi_card",  "title": "Total Revenue", "table": "sales",
                  "measure": "Total Revenue", "x": 20, "y": 20, "w": 390, "h": 120},
                 {"type": "bar_chart", "title": "Revenue by Region", "table": "sales",
                  "category_col": "Region", "measure": "Total Revenue",
                  "sorted_desc": true, "x": 20, "y": 160, "w": 590, "h": 510}
              ]
            }
          ]
        }

        Visual dict keys = the add_* parameter names. "type" selects the method.
        Returns the editor summary.
        """
        default_style = spec.get("style", DEFAULT_STYLE)
        if spec.get("theme", True):
            try:
                self.embed_theme(default_style, force=force)
            except FileNotFoundError as e:
                print(f"  [warn] theme not embedded: {e}")

        for page_spec in spec.get("pages", []):
            key = page_spec.get("page", 0)
            if isinstance(key, str) and key.startswith("new:"):
                page = self.add_page(key[4:])
            else:
                page = self.get_page(key)

            canvas = page_spec.get("canvas")
            if canvas:
                page.set_canvas_size(canvas.get("width"), canvas.get("height"))
            if page_spec.get("clear", True):
                page.clear_visuals()

            for vs in page_spec.get("visuals", []):
                vs = dict(vs)
                vtype = vs.pop("type")
                method = self._SPEC_TYPES.get(vtype)
                if method is None:
                    raise ValueError(f"Unknown visual type '{vtype}'. "
                                     f"Available: {sorted(set(self._SPEC_TYPES))}")
                vs.setdefault("style", default_style)
                # allow w/h aliases width/height in specs
                if "width" in vs:
                    vs["w"] = vs.pop("width")
                if "height" in vs:
                    vs["h"] = vs.pop("height")
                method_fn = getattr(page, method)
                params = inspect.signature(method_fn).parameters
                # a builder that takes "slot" itself owns the decision —
                # add_svg_card floats by default and the spec must be able to
                # turn that off
                floating = ("slot" not in params
                            and vs.pop("slot", True) is False)
                if "style" not in params:
                    vs.pop("style", None)
                if "force" in params:            # propagate build-level force
                    vs.setdefault("force", force)
                method_fn(**vs)
                if floating and not page._visuals[-1]["name"].startswith("ns_"):
                    page._visuals[-1]["name"] = "ns_" + page._visuals[-1]["name"][:17]

            # Optional competition-style background:
            #   "background": true
            #   or {"title": "...", "subtitle": "...", "nav_rail": 56, ...}
            bg = page_spec.get("background")
            if bg:
                if bg is True:
                    bg = {}
                page.generate_background(
                    header_title=bg.get("title"),
                    header_subtitle=bg.get("subtitle"),
                    header_height=bg.get("header_height", 90),
                    style=bg.get("style", default_style),
                    nav_rail=bg.get("nav_rail", 0),
                    shadow=bg.get("shadow"),
                    radius=bg.get("radius", 10),
                    header_reserve_right=bg.get("reserve_right", 0),
                    force=force,
                )

            page.save(force=force)

        return self.summary()

    def build_from_spec_file(self, path: str, force: bool = False) -> str:
        with open(path, encoding="utf-8") as f:
            return self.build_from_spec(json.load(f), force=force)

    # ── reporting ──────────────────────────────────────────────────────────────

    def summary(self) -> str:
        lines = [f"PBIR Report: {self.report_folder}"]
        if self.validate:
            user_tables = [t for t in self.model_index.tables
                           if not t.startswith(("LocalDateTable_", "DateTableTemplate_"))]
            lines.append(f"  Model: {len(user_tables)} table(s) — validation ON")
        else:
            lines.append("  Model: not found — validation OFF")
        try:
            folders = self._page_folders()
        except FileNotFoundError as e:
            return str(e)
        for folder in folders:
            pj   = self._read_page_json(folder)
            vdir = os.path.join(self.pages_folder, folder, "visuals")
            n    = len(os.listdir(vdir)) if os.path.exists(vdir) else 0
            lines.append(f"  Page '{pj.get('displayName', folder)}' "
                         f"({pj.get('width', '?')}x{pj.get('height', '?')}): {n} visuals")
        return "\n".join(lines)
