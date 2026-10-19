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

The SVG-measure path (below) is verified the same way, by
`python tests/build_svg_smoke.py`:

```
calculated tables  ok  _Measures, Theme
measures           ok  21 theme + 8 SVG (dataCategory ImageUrl)
visuals            ok  8 cardVisual + slicer + title
gate names         rc=0  NAMES OK — 1 model(s), 0 warning(s)
gate pbir          rc=0  ALL VALID
gate svg           rc=0  SVG MEASURES OK — 8 measure(s), 8 card(s), 0 warning(s)
```

That path is the product. Everything below is what stands between it and
running unattended on Linux.

## Layout

```
pbip_scaffold.py        CSV -> complete .pbip project
tmdl_editor.py          DAX measures + calculated tables into the model (TMDL)
pbir_editor.py          every visual, page and theme (PBIR JSON)
svg_measures.py         DAX measures that RETURN an SVG document  (see below)
design_measures.py      KPI context lines, trends, bar highlights
layout_templates.py     executive_hero / app_shell / story_page grids
bg_generator.py         designed page background PNG
brand_styles.py         a full style + Power BI theme from one brand color,
                        and the light/dark SVG colour-token pair
icon_generator.py       glyph icons as PNG
_validate_pbir.py       gate: report JSON against the PBIR schema  -> ALL VALID
_check_model_names.py   gate: TMDL measure/column name collisions  -> NAMES OK
_validate_svg_measures.py  gate: SVG measures parse and fit their cards
                        -> SVG MEASURES OK
themes/                 5 prebuilt Power BI themes
pbip_template/          Microsoft base theme shipped inside a project
tests/_test_v2.py       scaffold + TMDL + every visual family + templates
tests/_test_design.py   design layer: KPI tiles, DAX context, brand styles
tests/_test_svg.py      the SVG-measure path, including gate negative tests
tests/build_svg_smoke.py  a whole SVG-card project + all three gates
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

## SVG measure cards

The chart visuals above are Power BI's. This is the other path: **a DAX measure
returns an SVG document as a data URI, and a chrome-stripped `cardVisual`
renders it.** The visual IS the drawing — no chart visual, no custom visual,
nothing to install. It is how a report stops looking like Power BI.

```python
import svg_measures as S
from brand_styles import derive_theme_pair

pair = derive_theme_pair("#C8963C")                # light + dark palettes
S.add_theme_measures(model, pair["light"], pair["dark"], table="_Measures")

card = S.kpi_sparkbar(
    348, 119, "Hires closed",
    value=("Hires", 1284),                          # (measure, sample value)
    change=("Hires change label", "+8.2%"),
    icon="trend", series=S.SeriesSpec(
        {"@v": "CALCULATE ( [Hires], ... )", "@lab": "..."},
        [{"@v": 34, "@lab": "Apr"}, ...]),          # samples fix the mark count
    tokens=pair["light"])

S.add_svg_measure(model, "_Measures", "KPI hires (SVG)", card)
page.add_svg_card("_Measures", "KPI hires (SVG)", 40, 108, card.width, card.height)
```

`tests/build_svg_smoke.py` builds a whole project this way and runs all three
gates (the PBIR one skips when Microsoft's schemas are unreachable, as the
suites do — see gap 1). Its `samples/sheet.html` is every primitive, light over
dark, on one page.

### What the module emits is DAX, not SVG

Python composes the geometry — where the marks sit, how wide the bars are,
which font size fits — and returns a DAX string-concatenation expression that
redraws it from live values. Every scalar inside carries **both** its DAX
expression and its sample result, so the offline render and the live card come
from one pass over the same geometry. A primitive returns an `SvgCard`, which
unpacks as `(dax, width, height)` and also carries `.svg`, the sample render.

### Render it offline before Power BI ever sees it

A malformed SVG renders as a **silently blank card**: no error, no warning,
nothing in the UI. So every primitive can draw itself with no DAX engine
involved:

```python
S.render_sample(card, "out/kpi.svg")   # writes the file, asserts it is XML
```

### The third gate

```bash
python _validate_svg_measures.py out      # -> SVG MEASURES OK
```

It reads the model and the report and catches the four ways to get a blank or
wrong card, each of which Power BI reports as nothing at all:

| Failure | How it is caught |
|---|---|
| Malformed SVG | the literal skeleton of each document — and of each `CONCATENATEX` mark body, separately — is parsed as XML |
| `#` escaped before `%` | `%23` introduces a `%` that is then escaped again into `%2523` |
| SVG size ≠ card size | `preserveAspectRatio='none'` stretches rather than crops, so a mismatch distorts every mark |
| missing `dataCategory: ImageUrl` | the card prints the data URI as text |

It exits non-zero on failure. `tests/_test_svg.py` mutates a passing project six
ways and asserts the gate rejects every one — a gate that only ever passes is
not a gate.

### Themes are measures

`brand_styles.derive_theme_pair(brand_hex)` returns a light/dark pair of the 20
colour tokens (`page`, `card`, `ink`, `muted`, `accent`, `track`, `chip`, …;
`brand_styles.SVG_TOKENS` documents each). `svg_measures.add_theme_measures`
writes a two-row disconnected `Theme` table and one `Color <token>` measure per
token, each `IF([Selected theme] = "Dark", <dark>, <light>)`. Primitives name
tokens, never colours, so **one slicer click re-themes the whole report** — no
bookmarks, no second page, no rebuild. The contrast each palette promises is
asserted in the tests, not hoped for.

### The primitives

Each takes a token mapping and returns `(dax, width, height)`.

| Primitive | Draws | Animation |
|---|---|---|
| `kpi_sparkbar` | title, big number, change chip, column sparkbar | columns grow, staggered 0.07s |
| `gauge_arc` | track arc + value arc, value in the middle | the arc draws itself (dash offset) |
| `bar_list` | ranked rows: label, track, bar, value | bars grow, staggered 0.06s |
| `donut` | ring of slices, centre value, optional legend | slices draw in turn |
| `line_area` | line with area fill, gridlines, thinned x labels | the line draws itself, dots fade in |
| `progress_track` | labelled bar with an optional target tick | the fill grows |

### Text fitting is explicit, never incidental

SVG has no layout engine: text does not wrap, does not shrink and does not
clip — a long value simply draws past the edge of the card. So every primitive
documents its rule, and the rule is computed from the space available rather
than hardcoded:

* **Numbers** take the largest size from a ladder that still fits, decided at
  refresh time from the real string length — `SWITCH(TRUE(), LEN(x) <= n, "34",
  …)`, where each `n` is `floor(available_px / (size * em))`.
* **Labels** are truncated to the characters that fit, with an ellipsis.
* **Axis ticks** are thinned, not shrunk: every k-th label, counting back from
  the newest point so the period that matters is always labelled.
* A change chip flows after its number with a `tspan dx`, so a wider number
  pushes it along instead of being overwritten by it.

Model-derived text is XML-escaped in DAX before it reaches the document — one
client called "Smith & Co" is otherwise enough to blank a card.

### Known limits

* **The mark count is fixed when the measure is composed.** The geometry (bar
  pitch, slice step, point spacing) is computed in Python, so a series declares
  how many rows it draws. A top-5 bar list stays a top-5 bar list.
* **Font metrics are estimates.** Fitting uses measured per-character advances
  for Segoe UI (`EM_DIGIT`, `EM_LABEL`); a very different face will fit
  differently.
* **The gate reads literals, not values.** It validates the document skeleton
  with live expressions collapsed to a placeholder — it cannot know that a
  measure will return a negative width at refresh.
* **The document must be assembled in one VAR** for the gate to find it
  (`VAR _svg = "<svg …>" & … & "</svg>"`). Conditional documents — one per
  theme — are found too, and each is checked.

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
