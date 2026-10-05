"""
design_measures.py — DAX measures that drive dashboard design, generated
instead of hand-written.

The KPI tile (pbir_editor.add_kpi_tile) and the highlighted bar chart
(color_measure=) need measures that return TEXT and HEX COLORS, not numbers.
Getting their DAX right is fiddly (partial months, blank periods, "lower is
better" KPIs), so it lives here once:

    from tmdl_editor import TmdlModel
    from design_measures import add_kpi_context, add_highlight_color

    m = TmdlModel(paths["semantic_model"])
    ctx = add_kpi_context(m, "sales", "Total revenue",
                          date_col="order_date", month_col="order_month",
                          compare="mom", style="dark_analytics")
    # ctx = {"change": ..., "label": ..., "color": ..., "trend": ...}
    hl = add_highlight_color(m, "sales", "Total revenue", "region",
                             style="dark_analytics")

Then in the spec:
    {"type": "kpi_tile", "measure": "Total revenue", "icon": "money",
     "note_measure": ctx["label"], "note_color_measure": ctx["color"],
     "trend_col": "order_month", "trend_measure": ctx["trend"]}
    {"type": "bar_chart", ..., "color_measure": hl}

Honesty rules baked in (a KPI arrow is a claim — it must survive scrutiny):
  * Comparisons use COMPLETE months only. The data's last month is usually
    partial (a feed that ends on the 18th); comparing it with a full month
    manufactures a drop.
  * A comparison whose prior period is not fully covered by the data returns
    BLANK and the label says so — never a % against half a month.
  * Rates compare in percentage POINTS (compare_as="points"): "5.8% -> 7.9%"
    is "+2.1 pts", not "+36%".
  * higher_is_better=False flips the colors (cost, time-to-hire, churn).
  * Cohort caveat — NOT detectable here: if the date is when the record
    STARTED (applied, signed up), recent cohorts have not finished converting.
    Their hires/conversions are understated. Use counts of the cohort itself
    (applications) or say so in the dashboard.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pbir_editor import STYLES, DEFAULT_STYLE

FOLDER = "Design measures"

_COMPARE = ("mom", "yoy")


def _col(table: str, col: str) -> str:
    return f"'{table}'[{col}]"


def _style(style: str) -> dict:
    return STYLES.get(style or DEFAULT_STYLE, STYLES[DEFAULT_STYLE])


def _day(expr: str) -> str:
    """Strip the time part while staying a DATE (INT() would turn it into a
    number, and DAX refuses some date-vs-integer comparisons)."""
    return f"DATE(YEAR({expr}), MONTH({expr}), DAY({expr}))"


def _as_of_dax(as_of) -> str:
    """ISO date 'YYYY-MM-DD' -> DAX DATE(), validated."""
    import datetime
    dt = datetime.date.fromisoformat(as_of)
    return f"DATE({dt.year}, {dt.month}, {dt.day})"


def _windows(table: str, date_col: str, compare: str, as_of: str = None) -> str:
    """DAX VARs defining the current and prior windows (half-open [start, end))
    plus _covered: TRUE only when the data spans the whole prior window.
    as_of caps the latest date — see add_kpi_context."""
    d = _col(table, date_col)
    max_expr = (f"MIN(MAX({d}), {_as_of_dax(as_of)})" if as_of else f"MAX({d})")
    head = (f"VAR _dataMin = CALCULATE(MIN({d}), REMOVEFILTERS('{table}'))\n"
            f"VAR _maxDate = {max_expr}\n"
            f"VAR _minDate = MIN({d})\n")
    if compare == "mom":
        # last COMPLETE month in the selection vs the month before it
        return head + (
            f"VAR _lastDay = {_day('_maxDate')}\n"
            "VAR _curEnd = IF(_lastDay = EOMONTH(_lastDay, 0), _lastDay + 1,"
            " EOMONTH(_lastDay, -1) + 1)\n"
            "VAR _curStart = EOMONTH(_curEnd - 1, -1) + 1\n"
            "VAR _prevEnd = _curStart\n"
            "VAR _prevStart = EOMONTH(_prevEnd - 1, -1) + 1\n"
            f"VAR _covered = {_day('_dataMin')} <= _prevStart\n")
    if compare == "yoy":
        # the selected period vs the same dates one year earlier
        return head + (
            f"VAR _curStart = {_day('_minDate')}\n"
            f"VAR _curEnd = {_day('_maxDate')} + 1\n"
            "VAR _prevStart = EDATE(_curStart, -12)\n"
            "VAR _prevEnd = EDATE(_curEnd, -12)\n"
            f"VAR _covered = {_day('_dataMin')} <= _prevStart\n")
    raise ValueError(f"compare must be one of {_COMPARE}, got {compare!r}")


def add_kpi_context(model, table: str, measure: str, date_col: str,
                    month_col: str = None, compare: str = "mom",
                    compare_as: str = "percent", higher_is_better: bool = True,
                    extra_date_cols: list = None, style: str = None,
                    as_of: str = None,
                    prefix: str = None, force: bool = False) -> dict:
    """
    Create the measures behind a KPI tile's context line and trend.

    table/measure : the KPI's base measure (must already exist)
    date_col      : the row-level date column the comparison windows use
    month_col     : month-start column for the trend (enables "trend")
    compare       : "mom" — last complete month vs the month before
                    "yoy" — selected period vs the same dates a year earlier
    compare_as    : "percent" (relative change) | "points" (for rates)
    higher_is_better: False for cost / duration / churn KPIs (flips colors)
    extra_date_cols: other date-derived columns whose filters must be cleared
                    too (month_col is added automatically)
    as_of         : 'YYYY-MM-DD' extract date. Use it when the feed carries
                    future-dated rows (data-entry defects): without it the
                    "latest month" is wherever the stray row landed (a June
                    2027 lead in a feed extracted Sep 2026 turns the tile into
                    "Jun vs May 2027" on one record). Also caps the trend.

    Returns {"change", "label", "color", "trend"(if month_col)} measure names.
    """
    if compare_as not in ("percent", "points"):
        raise ValueError("compare_as must be 'percent' or 'points'")
    s = _style(style)
    base = f"[{measure}]"
    p = prefix or measure
    names = {"change": f"{p} change", "label": f"{p} change label",
             "color": f"{p} change color"}

    clear = [date_col] + ([month_col] if month_col else []) + list(extra_date_cols or [])
    clear_dax = ", ".join(_col(table, c) for c in dict.fromkeys(clear))
    d = _col(table, date_col)
    win = _windows(table, date_col, compare, as_of)
    calc = (f"VAR _cur = CALCULATE({base}, REMOVEFILTERS({clear_dax}),"
            f" {d} >= _curStart, {d} < _curEnd)\n"
            f"VAR _prev = CALCULATE({base}, REMOVEFILTERS({clear_dax}),"
            f" {d} >= _prevStart, {d} < _prevEnd)\n")
    diff = ("_cur - _prev" if compare_as == "points"
            else "DIVIDE(_cur - _prev, _prev)")
    change_dax = (win + calc +
                  "RETURN IF(_covered && NOT ISBLANK(_cur) && NOT ISBLANK(_prev),"
                  f" {diff})")
    how = ("last complete month vs the month before" if compare == "mom"
           else "selected period vs the same dates a year earlier")
    model.add_measure(table, names["change"], change_dax,
                      format_string="+0.0%;-0.0%;0.0%",
                      display_folder=FOLDER, force=force,
                      description=f"{measure}: {how}. BLANK when the prior "
                                  "period is not fully covered by the data.")

    if compare == "mom":
        period = ('FORMAT(_curStart, "mmm") & " vs " & FORMAT(_prevStart, "mmm")')
        empty = '"No complete prior month"'
    else:
        period = '"vs last year"'
        empty = '"No data for last year"'
    amount = ('FORMAT(ABS(_chg) * 100, "0.0") & " pts"' if compare_as == "points"
              else 'FORMAT(ABS(_chg), "0.0%")')
    label_dax = (win +
                 f"VAR _chg = [{names['change']}]\n"
                 "RETURN IF(ISBLANK(_chg), " + empty + ",\n"
                 '    IF(_chg > 0, "▲ ", IF(_chg < 0, "▼ ", "■ ")) & ' + amount +
                 ' & " " & ' + period + ")")
    model.add_measure(table, names["label"], label_dax,
                      display_folder=FOLDER, force=force, text_measure=True,
                      description=f"Display text for the {measure} KPI tile context line.")

    up, down = (s["positive"], s["negative"]) if higher_is_better \
        else (s["negative"], s["positive"])
    color_dax = (f"VAR _chg = [{names['change']}]\n"
                 "RETURN SWITCH(TRUE(),\n"
                 f'    ISBLANK(_chg) || _chg = 0, "{s["text_secondary"]}",\n'
                 f'    _chg > 0, "{up}",\n'
                 f'    "{down}")')
    model.add_measure(table, names["color"], color_dax,
                      display_folder=FOLDER, force=force, text_measure=True,
                      description=f"Hex color for the {measure} context line "
                                  f"({'higher' if higher_is_better else 'lower'} is better).")

    if month_col:
        names["trend"] = f"{p} trend"
        m = _col(table, month_col)
        sel_max = (f"MIN(CALCULATE(MAX({d}), ALLSELECTED('{table}')), {_as_of_dax(as_of)})"
                   if as_of else f"CALCULATE(MAX({d}), ALLSELECTED('{table}'))")
        trend_dax = (
            f"VAR _selMin = CALCULATE(MIN({d}), ALLSELECTED('{table}'))\n"
            f"VAR _selMax = {sel_max}\n"
            f"VAR _mStart = EOMONTH(MIN({m}), -1) + 1\n"
            f"VAR _mEnd = EOMONTH(MAX({m}), 0)\n"
            f"RETURN IF(_mStart < {_day('_selMin')} || _mEnd > {_day('_selMax')}, BLANK(),"
            f" {base})")
        fmt = _format_of(model, table, measure)
        model.add_measure(table, names["trend"], trend_dax,
                          format_string=fmt, display_folder=FOLDER, force=force,
                          description=f"{measure} by month, complete months only "
                                      "(a partial first/last month would fake a dip).")
    return names


def add_highlight_color(model, table: str, measure: str, category_col,
                        name: str = None, top_n: int = 1,
                        direction: str = "top", style: str = None,
                        force: bool = False) -> str:
    """
    Color measure for bar/column charts: the top (or bottom) N categories by
    `measure` get the style accent, the rest the muted color — so the bar that
    answers the chart title stands out. Filter-aware (ALLSELECTED), so the
    highlight follows slicers instead of being baked in.

    category_col: column name, or (table, column) when it lives in a dimension
    direction:    "top" (largest) | "bottom" (smallest — e.g. fastest time)
    Pass the returned name as color_measure= on add_bar_chart/add_column_chart.
    """
    if direction not in ("top", "bottom"):
        raise ValueError("direction must be 'top' or 'bottom'")
    s = _style(style)
    ct, cc = category_col if isinstance(category_col, (tuple, list)) else (table, category_col)
    name = name or f"{measure} highlight"
    order = "DESC" if direction == "top" else "ASC"
    dax = (f"VAR _v = [{measure}]\n"
           f"VAR _rank = RANKX(ALLSELECTED({_col(ct, cc)}), [{measure}], , {order}, Dense)\n"
           f'RETURN IF(NOT ISBLANK(_v) && _rank <= {int(top_n)}, "{s["accent"]}", "{s["muted"]}")')
    model.add_measure(table, name, dax, display_folder=FOLDER, force=force,
                      text_measure=True,
                      description=f"Bar color: {direction} {top_n} {cc} by {measure} "
                                  "in the accent, the rest muted.")
    return name


def _format_of(model, table: str, measure: str):
    """The base measure's formatString, so the trend tooltip matches the KPI."""
    tf = model._table_file(table)
    block = tf.measure_block(measure)
    if not block:
        return None
    for i in range(block[0], block[1]):
        line = tf.lines[i].strip()
        if line.startswith("formatString:"):
            return line.split(":", 1)[1].strip()
    return None


def add_data_through(model, table: str, date_col: str, name: str = "Data through",
                     date_format: str = "d mmm yyyy", force: bool = False) -> str:
    """
    Text measure for a menu footer: "Data through 18 Sep 2026" — the latest
    date in the WHOLE table (filters removed), so it answers "how fresh is
    this?" and stays true after every refresh, unlike a typed text box.
    """
    d = _col(table, date_col)
    dax = (f'"Data through " & FORMAT(CALCULATE(MAX({d}), REMOVEFILTERS(\'{table}\')),'
           f' "{date_format}")')
    model.add_measure(table, name, dax, display_folder=FOLDER, force=force,
                      text_measure=True,
                      description="Freshness line for the report menu footer.")
    return name


def add_top_insight(model, table: str, measure: str, category_col,
                    noun: str = "", name: str = None, value_format: str = "#,0",
                    template: str = "{top} leads with {value} of {total} {noun} ({share})",
                    force: bool = False) -> str:
    """
    Insight sentence for add_insight_panel: the top category by `measure` and
    its share of the selection — "Engineering leads with 69 of 223 hires (31%)".
    Computed in DAX, so it follows slicers and is checkable against the data.

    template placeholders: {top} {value} {total} {share} {noun}
    category_col: column name, or (table, column) for a dimension
    """
    import re
    ct, cc = category_col if isinstance(category_col, (tuple, list)) else (table, category_col)
    name = name or f"Insight: top {cc} by {measure}"
    parts = {
        "top": "_top",
        "value": f'FORMAT(_v, "{value_format}")',
        "total": f'FORMAT(_all, "{value_format}")',
        "share": 'FORMAT(DIVIDE(_v, _all), "0%")',
        "noun": '"' + noun.replace('"', '""') + '"',
    }
    pieces = []
    for tok in re.split(r"(\{\w+\})", template.strip()):
        if not tok:
            continue
        key = tok[1:-1] if tok.startswith("{") and tok.endswith("}") else None
        if key and key in parts:
            pieces.append(parts[key])
        elif key:
            raise ValueError(f"unknown placeholder {tok}; use {sorted(parts)}")
        else:
            pieces.append('"' + tok.replace('"', '""') + '"')
    dax = (f"VAR _t = TOPN(1, ALLSELECTED({_col(ct, cc)}), [{measure}], DESC)\n"
           f"VAR _top = MAXX(_t, {_col(ct, cc)})\n"
           f"VAR _v = MAXX(_t, [{measure}])\n"
           f"VAR _all = CALCULATE([{measure}], ALLSELECTED({_col(ct, cc)}))\n"
           "RETURN IF(ISBLANK(_v), BLANK(), " + " & ".join(pieces) + ")")
    model.add_measure(table, name, dax, display_folder=FOLDER, force=force,
                      text_measure=True,
                      description=f"Insight sentence: top {cc} by {measure}.")
    return name
