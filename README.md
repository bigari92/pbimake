# pbimake

Build a complete, openable Power BI project (`.pbip`) from a CSV — scaffold, DAX
measures, visuals, theme and designed background — with **no Power BI Desktop
and no Windows UI involved**. Desktop is only needed at the end, by the person
receiving the file, to build its data cache on first refresh.

Extracted from a larger private toolkit so the headless build path can be worked
on in a clean environment. Nothing in this repo touches bidding, pricing,
client analytics or any customer data.

## Status — verified headless on this commit

A full build was run end to end from this folder alone, with Power BI Desktop
closed and no external theme directory:

```
imports        ok  (pbir_editor v2.1)
THEMES_DIR     <repo>/themes          5 themes found
scaffold       ok  smoke.Report
measures       ok  2 written
visuals        ok  3 built
gate names     rc=0  NAMES OK — 1 model(s), 0 warning(s)
gate pbir      rc=0  ALL VALID
```

That path is the product. Everything below is what stands between it and
running unattended on Linux.

## Layout

```
pbip_scaffold.py        CSV -> complete .pbip project
tmdl_editor.py          DAX measures into the semantic model (TMDL)
pbir_editor.py          every visual, page and theme (PBIR JSON)
design_measures.py      KPI context lines, trends, bar highlights
layout_templates.py     executive_hero / app_shell / story_page grids
bg_generator.py         designed page background PNG
brand_styles.py         a full style + Power BI theme from one brand color
icon_generator.py       glyph icons as PNG
_validate_pbir.py       gate: report JSON against the PBIR schema  -> ALL VALID
_check_model_names.py   gate: TMDL measure/column name collisions  -> NAMES OK
themes/                 5 prebuilt Power BI themes
pbip_template/          Microsoft base theme shipped inside a project
tests/_test_v2.py       scaffold + TMDL + every visual family + templates
tests/_test_design.py   design layer: KPI tiles, DAX context, brand styles
tests/generate_data.py  seeded synthetic hiring-funnel CSV (never committed)
tests/fixtures/         make_fixture.py + golden/ — the regression net
```

Modules import each other by bare name, so run with the repo root on
`sys.path`. There is no package wrapper yet — deliberately, so the extraction
changed as little as possible.

## Running it

```bash
pip install -r requirements.txt
```

```python
import sys; sys.path.insert(0, "<repo root>")
import pbip_scaffold
from tmdl_editor import TmdlModel
from pbir_editor import PbirEditor

paths = pbip_scaffold.new_project("out", "sales", "sales.csv")
m = TmdlModel(paths["semantic_model"])
m.add_measure("sales", "Total revenue", "SUM('sales'[revenue])", format_string="$#,0")

ed = PbirEditor(paths["report"])
ed.build_from_spec({"pages": [{"name": "Overview", "clear": True, "visuals": [
    {"type": "card_measure", "table": "sales", "measure": "Total revenue",
     "title": "Total revenue", "x": 40, "y": 40, "w": 260, "h": 120},
]}]})
```

Then both gates, which must print `NAMES OK` and `ALL VALID`:

```bash
python _check_model_names.py out
python _validate_pbir.py out/sales.Report
```

## Known gaps — the work queue

Ordered by what blocks an unattended Linux run.

### 1. Tests — done, with one honest limit

Both suites run unattended with **no environment variables, no Power BI and no
Windows**:

```bash
python tests/_test_v2.py        # 86 checks
python tests/_test_design.py    # 92 checks
```

`tests/fixtures/make_fixture.py` is the regression net. It generates the CSV
(seeded, so reproducible), scaffolds a project, writes a representative model
and builds one visual per family — card, kpi_tile, bar, line, funnel, scatter,
treemap, table — then compares every emitted TMDL and `visual.json` against the
golden copies in `tests/fixtures/golden/`:

```bash
python tests/fixtures/make_fixture.py                # build + compare
python tests/fixtures/make_fixture.py --write-golden # re-record after an intended change
```

**What the goldens prove, and what they do not.** They were produced by this
repo's own builders, so they can only show that the emitted JSON/TMDL has not
*changed*. They cannot show that it is what Power BI expects, because nothing
in the suite ever opened the file. Checking the output against a
Desktop-authored project stays a Windows job.

Two things the suites deliberately report as SKIP rather than PASS:

- **Glyph icons.** `add_icon` and the background's icon glyphs need Segoe MDL2
  Assets (gap 2 below). Geometry — icon tile, logo tile, nav rail — is still
  checked; only the glyph drawn inside it is not.
- **The PBIR schema gate.** `_validate_pbir.py` fetches Microsoft's schemas
  over HTTPS and caches them in `_schema_cache/`, which is gitignored. With no
  warm cache and no route to `developer.microsoft.com` it cannot run at all, so
  the suite skips it instead of reporting a false pass. Either give the test
  machine network once, or decide to commit the schema cache.

### 2. Fonts — a decision, not a patch

`icon_generator.py` requires **Segoe MDL2 Assets** from `%WINDIR%\Fonts` and
raises a deliberate, well-worded `FileNotFoundError` when absent, because a
substituted font renders tofu boxes rather than icons. `bg_generator.py` wants
Segoe UI and falls back to `ImageFont.load_default()`, which will not match
Segoe's metrics — the background layout maths may shift.

Neither font is redistributable. Pick one: ship a PNG icon set, or move to an
open icon font and an open UI face, and re-check the background geometry.

### 3. Five patch scripts were never backported

The reference build of the hiring-funnel dashboard needed five `patch_*.py`
passes after `build_from_spec`, which means **a plain spec build does not
currently reproduce the shipped artifact**. The three defects:

- `labelDisplayUnits` is never emitted by the chart/card builders, so large
  values round wrongly on cards (this shipped €1,740,783 as "€2M")
- `_objs_funnel` sets `categoryAxis show=false`
- `add_scatter_chart` emits no `categoryLabels` and no `bubbleSize`

Fix them in `pbir_editor.py` so the patches become unnecessary.

### 4. Smaller, mechanical

- `sys.stdout.reconfigure(encoding="utf-8")` at import — console output
  currently mangles em-dashes under cp1252
- `pbip_scaffold._m_escape` does not escape `#(`, which M treats as an escape
- `pbip_scaffold.new_project` leaves a half-written directory on failure, and
  the resulting `FileExistsError` then blocks every retry
- `infer_schema` samples 5,000 rows; a type that changes later in a large file
  is silently mis-modelled
- no schema over the spec dict — `build_from_spec` calls `method_fn(**vs)`
  bare, so a wrong key surfaces as `TypeError: … unexpected keyword argument`

### 5. Not solved anywhere yet

- **Cleaning.** There is no ingest module. Blank headers become `Unnamed: 6` in
  pandas and `Column7` in Power Query; the type map then matches nothing and
  refresh fails — and both gates still pass, so the failure is invisible until
  someone opens the file.
- **Verification.** Nothing compares a rendered number against the source data.
- **Portability of the output.** The generated M hardcodes an absolute CSV path
  (`pbip_scaffold.py:139`), so a project sent to another machine cannot refresh.

## What is deliberately absent

No data. Fixtures are generated, never committed — see `.gitignore`.
