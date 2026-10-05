"""
pbip_scaffold.py — generate a complete Power BI Project (.pbip) from a CSV,
without opening Power BI Desktop.

This eliminates the Computer-Use data-loading step (open PBI -> Get Data ->
CSV -> Load -> Save As .pbip). The generated project contains:

  <project_dir>/
    <name>.pbip                      launcher
    .gitignore
    <name>.Report/                   report skeleton (1 empty page) + base theme
    <name>.SemanticModel/            TMDL model with an M partition -> your CSV

Pipeline after scaffolding (Power BI stays CLOSED until the very end):
  1. tmdl_editor.TmdlModel(...).add_measure(...)     # DAX measures + formatString
  2. pbir_editor.PbirEditor(...).build_from_spec(...) or add_* calls
  3. Open the .pbip in Power BI Desktop -> Home -> Refresh (loads the CSV)
     -> Ctrl+S -> screenshot. Done.

The first open shows blank visuals until Refresh — that is expected: the
scaffold ships no data cache (.pbi/cache.abf); Refresh builds it.

Column types are inferred with pandas (int64 / double / dateTime / boolean /
string). String columns that parse as dates (>=95% of sampled values) become
dateTime.

File formats mirror a Power BI Desktop 2026 "Save As .pbip" byte-for-byte
(schemas: report 3.3.0 / page 2.1.0 / pagesMetadata 1.1.0, pbism 4.2,
pbir 4.0, TMDL compatibilityLevel 1600).
"""

import os, json, shutil, uuid, re

__version__ = "1.0"

TEMPLATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pbip_template")
_BASE_THEME_NAME = "CY26SU05"

EOL = "\r\n"

GITIGNORE = "**/.pbi/localSettings.json\n**/.pbi/cache.abf\n_archive/\n"


# ── CSV schema inference ───────────────────────────────────────────────────────

def infer_schema(csv_path: str, sample_rows: int = 5000,
                 delimiter: str = ",") -> list:
    """
    [(column_name, kind), ...] where kind is one of:
    "int64" | "double" | "boolean" | "date" | "datetime" | "string"
    """
    import warnings
    import pandas as pd

    warnings.filterwarnings("ignore", message="Could not infer format")
    df = pd.read_csv(csv_path, nrows=sample_rows, sep=delimiter)
    out = []
    for col in df.columns:
        s = df[col]
        kind = "string"
        if pd.api.types.is_bool_dtype(s):
            kind = "boolean"
        elif pd.api.types.is_integer_dtype(s):
            kind = "int64"
        elif pd.api.types.is_float_dtype(s):
            # integer column with NaNs comes through as float — keep double (safe)
            kind = "double"
        elif pd.api.types.is_datetime64_any_dtype(s):
            kind = "datetime"
        else:
            sample = s.dropna().astype(str).head(200)
            if len(sample) > 0:
                try:
                    parsed = pd.to_datetime(sample, errors="coerce")
                except Exception:
                    parsed = None
                if parsed is not None and parsed.notna().mean() >= 0.95:
                    kind = "datetime"
        if kind == "datetime":
            try:
                parsed = pd.to_datetime(s.dropna().astype(str).head(200), errors="coerce").dropna()
                if len(parsed) and (parsed.dt.normalize() == parsed).all():
                    kind = "date"
            except Exception:
                pass
        out.append((str(col), kind))
    return out


# ── TMDL generation ────────────────────────────────────────────────────────────

_KIND_TMDL = {
    # kind     -> (dataType,  summarizeBy, formatString, m_type)
    "int64":    ("int64",    "sum",  "0",         "Int64.Type"),
    "double":   ("double",   "sum",  None,        "type number"),
    "boolean":  ("boolean",  "none", None,        "type logical"),
    "date":     ("dateTime", "none", "Long Date", "type date"),
    "datetime": ("dateTime", "none", "General Date", "type datetime"),
    "string":   ("string",   "none", None,        "type text"),
}


def _quote_tmdl(name: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_]+", name):
        return name
    return "'" + name.replace("'", "''") + "'"


def _m_escape(s: str) -> str:
    return s.replace('"', '""')


def _table_tmdl(table_name: str, schema: list, csv_path: str,
                delimiter: str, encoding: int) -> str:
    L = []
    L.append(f"table {_quote_tmdl(table_name)}")
    L.append(f"\tlineageTag: {uuid.uuid4()}")
    L.append("")

    for col, kind in schema:
        data_type, summarize, fmt, _ = _KIND_TMDL[kind]
        L.append(f"\tcolumn {_quote_tmdl(col)}")
        L.append(f"\t\tdataType: {data_type}")
        if fmt:
            L.append(f"\t\tformatString: {fmt}")
        L.append(f"\t\tlineageTag: {uuid.uuid4()}")
        L.append(f"\t\tsummarizeBy: {summarize}")
        L.append(f"\t\tsourceColumn: {col}")
        L.append("")
        L.append("\t\tannotation SummarizationSetBy = Automatic")
        L.append("")
        if kind == "date":
            L.append("\t\tannotation UnderlyingDateTimeDataType = Date")
            L.append("")

    type_pairs = ", ".join(
        '{"%s", %s}' % (_m_escape(col), _KIND_TMDL[kind][3]) for col, kind in schema
    )
    csv_m = _m_escape(os.path.abspath(csv_path))

    L.append(f"\tpartition {_quote_tmdl(table_name)} = m")
    L.append("\t\tmode: import")
    L.append("\t\tsource =")
    L.append("\t\t\t\tlet")
    L.append(f'\t\t\t\t    Source = Csv.Document(File.Contents("{csv_m}"),'
             f'[Delimiter="{delimiter}", Columns={len(schema)}, '
             f'Encoding={encoding}, QuoteStyle=QuoteStyle.Csv]),')
    L.append('\t\t\t\t    #"Promoted Headers" = Table.PromoteHeaders(Source, [PromoteAllScalars=true]),')
    L.append('\t\t\t\t    #"Changed Type" = Table.TransformColumnTypes(#"Promoted Headers",{%s})'
             % type_pairs)
    L.append("\t\t\t\tin")
    L.append('\t\t\t\t    #"Changed Type"')
    L.append("")
    L.append("\tannotation PBI_ResultType = Table")
    L.append("")
    return EOL.join(L)


def _model_tmdl(table_names: list) -> str:
    query_order = ", ".join(f'"{t}"' for t in table_names)
    L = [
        "model Model",
        "\tculture: en-US",
        "\tdefaultPowerBIDataSourceVersion: powerBI_V3",
        "\tsourceQueryCulture: en-US",
        "\tdataAccessOptions",
        "\t\tlegacyRedirects",
        "\t\treturnErrorValuesAsNull",
        "",
        "annotation __PBI_TimeIntelligenceEnabled = 1",
        "",
        f"annotation PBI_QueryOrder = [{query_order}]",
        "",
        'annotation PBI_ProTooling = ["DevMode"]',
        "",
    ]
    for t in table_names:
        L.append(f"ref table {_quote_tmdl(t)}")
        L.append("")
    L.append("ref cultureInfo en-US")
    L.append("")
    return EOL.join(L)


# ── JSON boilerplate ───────────────────────────────────────────────────────────

def _platform_json(item_type: str, display_name: str) -> dict:
    return {
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/gitIntegration/platformProperties/2.0.0/schema.json",
        "metadata": {"type": item_type, "displayName": display_name},
        "config": {"version": "2.0", "logicalId": str(uuid.uuid4())},
    }


def _report_json() -> dict:
    return {
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/report/3.3.0/schema.json",
        "themeCollection": {
            "baseTheme": {
                "name": _BASE_THEME_NAME,
                "reportVersionAtImport": {
                    "visual": "2.9.0", "report": "3.3.0", "page": "2.3.1",
                },
                "type": "SharedResources",
            }
        },
        "objects": {
            "section": [
                {"properties": {"verticalAlignment": {"expr": {"Literal": {"Value": "'Top'"}}}}}
            ]
        },
        "resourcePackages": [
            {
                "name": "SharedResources",
                "type": "SharedResources",
                "items": [
                    {"name": _BASE_THEME_NAME,
                     "path": f"BaseThemes/{_BASE_THEME_NAME}.json",
                     "type": "BaseTheme"}
                ],
            }
        ],
        "settings": {
            "useStylableVisualContainerHeader": True,
            "exportDataMode": "AllowSummarized",
            "defaultDrillFilterOtherVisuals": True,
            "allowChangeFilterTypes": True,
            "useEnhancedTooltips": True,
            "useDefaultAggregateDisplayName": True,
        },
    }


def _write_json(path: str, obj: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)


def _write_text(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)


# ── Public API ─────────────────────────────────────────────────────────────────

def new_project(project_dir: str, name: str, csv_path: str,
                table_name: str = None, page_name: str = "Page 1",
                canvas: tuple = (1280, 720),
                delimiter: str = ",", encoding: int = 65001,
                extra_csvs: list = None,
                allow_onedrive: bool = False) -> dict:
    """
    Create a complete .pbip project from one or more CSVs. Power BI not needed.

    project_dir : folder to create the project in (created if missing)
    name        : project name -> <name>.pbip, <name>.Report, <name>.SemanticModel
    csv_path    : the main (fact) CSV to load (absolute path recommended)
    table_name  : model table name (default: CSV filename without extension)
    extra_csvs  : additional tables for star-schema models — list of CSV paths
                  or {"csv": path, "table": name} dicts. Add relationships after
                  scaffolding: tmdl_editor.TmdlModel(...).add_relationship(...)
    canvas      : (width, height) of the first page
    encoding    : M codepage — 65001 = UTF-8 (pandas to_csv default), 1252 = Windows-1252

    Returns {"pbip", "report", "semantic_model", "table", "tables"}

    After scaffolding: add measures + relationships (tmdl_editor), build visuals
    (pbir_editor), then open the .pbip ONCE -> Refresh -> Ctrl+S -> screenshot.
    """
    if "onedrive" in project_dir.lower() and not allow_onedrive:
        raise ValueError(
            "Refusing to scaffold inside a OneDrive-synced folder (file-locking "
            "breaks Power BI saves). Use a local path like C:\\Users\\valer\\portfolio\\... "
            "or pass allow_onedrive=True."
        )
    csv_path = os.path.abspath(csv_path)
    if not os.path.isfile(csv_path):
        raise FileNotFoundError(f"CSV not found: {csv_path}")
    if table_name is None:
        table_name = os.path.splitext(os.path.basename(csv_path))[0]

    # normalize the full table list: [(table, csv_abspath), ...]
    tables = [(table_name, csv_path)]
    for extra in (extra_csvs or []):
        if isinstance(extra, dict):
            ec, et = extra["csv"], extra.get("table")
        else:
            ec, et = extra, None
        ec = os.path.abspath(ec)
        if not os.path.isfile(ec):
            raise FileNotFoundError(f"CSV not found: {ec}")
        et = et or os.path.splitext(os.path.basename(ec))[0]
        if et in (t for t, _ in tables):
            raise ValueError(f"Duplicate table name '{et}' — pass an explicit table name.")
        tables.append((et, ec))

    report_dir = os.path.join(project_dir, f"{name}.Report")
    model_dir  = os.path.join(project_dir, f"{name}.SemanticModel")
    pbip_path  = os.path.join(project_dir, f"{name}.pbip")
    for d in (report_dir, model_dir):
        if os.path.exists(d):
            raise FileExistsError(
                f"{d} already exists — refusing to overwrite an existing project.")

    schemas = {}
    for t, c in tables:
        schemas[t] = infer_schema(c, delimiter=delimiter)
        print(f"  Schema inferred from {os.path.basename(c)} -> table '{t}':")
        for col, kind in schemas[t]:
            print(f"    {col}: {kind}")

    # ── SemanticModel ──────────────────────────────────────────────────────────
    _write_json(os.path.join(model_dir, ".platform"),
                _platform_json("SemanticModel", name))
    _write_json(os.path.join(model_dir, "definition.pbism"),
                {"version": "4.2", "settings": {}})
    _write_text(os.path.join(model_dir, "definition", "database.tmdl"),
                "database" + EOL + "\tcompatibilityLevel: 1600" + EOL + EOL)
    _write_text(os.path.join(model_dir, "definition", "model.tmdl"),
                _model_tmdl([t for t, _ in tables]))
    _write_text(os.path.join(model_dir, "definition", "cultures", "en-US.tmdl"),
                "cultureInfo en-US" + EOL)
    for t, c in tables:
        _write_text(os.path.join(model_dir, "definition", "tables", f"{t}.tmdl"),
                    _table_tmdl(t, schemas[t], c, delimiter, encoding))

    # ── Report ─────────────────────────────────────────────────────────────────
    _write_json(os.path.join(report_dir, ".platform"),
                _platform_json("Report", name))
    _write_json(os.path.join(report_dir, "definition.pbir"), {
        "version": "4.0",
        "datasetReference": {"byPath": {"path": f"../{name}.SemanticModel"}},
    })
    _write_json(os.path.join(report_dir, "definition", "report.json"), _report_json())
    _write_json(os.path.join(report_dir, "definition", "version.json"), {
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/versionMetadata/1.0.0/schema.json",
        "version": "2.0.0",
    })

    page_id = uuid.uuid4().hex[:20]
    _write_json(os.path.join(report_dir, "definition", "pages", "pages.json"), {
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/pagesMetadata/1.1.0/schema.json",
        "pageOrder": [page_id],
        "activePageName": page_id,
    })
    _write_json(os.path.join(report_dir, "definition", "pages", page_id, "page.json"), {
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/page/2.1.0/schema.json",
        "name": page_id,
        "displayName": page_name,
        "displayOption": "FitToPage",
        "height": canvas[1],
        "width": canvas[0],
    })

    # Base theme resource (PBI expects the file the report.json references)
    theme_src = os.path.join(TEMPLATE_DIR, "BaseThemes", f"{_BASE_THEME_NAME}.json")
    if not os.path.isfile(theme_src):
        raise FileNotFoundError(
            f"Base theme template missing: {theme_src} — copy any "
            "<proj>.Report/StaticResources/SharedResources/BaseThemes/CY*.json "
            "into pbip_template/BaseThemes/.")
    theme_dst = os.path.join(report_dir, "StaticResources", "SharedResources",
                             "BaseThemes", f"{_BASE_THEME_NAME}.json")
    os.makedirs(os.path.dirname(theme_dst), exist_ok=True)
    shutil.copyfile(theme_src, theme_dst)

    # ── Launcher + housekeeping ────────────────────────────────────────────────
    _write_json(pbip_path, {
        "version": "1.0",
        "artifacts": [{"report": {"path": f"{name}.Report"}}],
        "settings": {"enableAutoRecovery": True},
    })
    gi = os.path.join(project_dir, ".gitignore")
    if not os.path.exists(gi):
        _write_text(gi, GITIGNORE)

    print(f"  Scaffolded: {pbip_path}")
    for t, c in tables:
        print(f"  Table '{t}' <- {c}")
    print("  Next: add measures/relationships (tmdl_editor) + visuals (pbir_editor), then")
    print("  open the .pbip in PBI -> Refresh -> Ctrl+S. First open is blank until Refresh.")

    return {
        "pbip": pbip_path,
        "report": report_dir,
        "semantic_model": model_dir,
        "table": table_name,
        "tables": [t for t, _ in tables],
    }
