"""
layout_templates.py — competition-grade page layout presets for pbir_editor.

Each template is a PURE function: it takes the confirmed dashboard content
(KPIs, hero visual, supporting charts...) and returns a build_from_spec dict
with every x/y/w/h computed on the layout grid — plus the designed background,
KPI icons, and (optionally) a page navigator. No hand-placed coordinates.

    from layout_templates import executive_hero
    spec = executive_hero(style="dark_analytics", table="funnel_clean",
                          header={"title": "...", "subtitle": "..."},
                          kpis=[{"title": "Total Sessions",
                                 "measure": "Total Sessions", "icon": "people"}],
                          hero={"type": "funnel", "title": "...",
                                "category_col": "funnel_event_name",
                                "value_col": "funnel_event_name", "agg": "count"},
                          supporting=[...], detail=[...])
    ed.build_from_spec(spec)          # then save spec as dashboard_spec.json

Design system (from the competition research):
  1280px canvas · 20px margins & gutters · 90px header band · KPI row 120px ·
  visual hierarchy (hero LARGER than supporting — equal tiles read as basic).
Visual dicts are ordinary spec entries WITHOUT x/y/w/h (the template fills them).
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pbir_editor import STYLES, DEFAULT_STYLE

CANVAS_W  = 1280
MARGIN    = 20
GUTTER    = 20
HEADER_H  = 90
CONTENT_Y = HEADER_H + GUTTER          # first row under the header band
KPI_H     = 120
KPI_TILE_H = 160                       # rich tiles: value + context line + trend
ICON_SIZE = 28
TILE_KEYS = ("note_measure", "trend_col")   # any of these -> kpi_tile


def _accent(style: str) -> str:
    return STYLES.get(style or DEFAULT_STYLE, STYLES[DEFAULT_STYLE])["accent"]

def _place(visual: dict, x: int, y: int, w: int, h: int) -> dict:
    v = dict(visual)
    v.update(x=int(x), y=int(y), w=int(w), h=int(h))
    return v

def _row(items: list, y: int, h: int, x0: int = MARGIN,
         width: int = CANVAS_W - 2 * MARGIN) -> list:
    """Distribute items evenly across one row."""
    n = len(items)
    w = (width - (n - 1) * GUTTER) / n
    return [_place(it, x0 + i * (w + GUTTER), y, w, h) for i, it in enumerate(items)]

def _kpi_row(kpis: list, slicer: dict, style: str, y: int,
             x0: int = MARGIN, width: int = CANVAS_W - 2 * MARGIN) -> tuple:
    """KPI cards (+ optional right-aligned slicer). Returns (visuals, row_height).

    A KPI with a context line or trend (note_measure / trend_col — see
    design_measures.add_kpi_context) becomes a rich kpi_tile: colored icon
    tile + value + "▲ 4.2% Aug vs Jul" + sparkline, and the whole row grows to
    KPI_TILE_H so every card in it lines up. Plain KPIs keep the classic card
    with a corner icon."""
    if not 1 <= len(kpis) <= 4:
        raise ValueError(f"kpis: expected 1-4 cards, got {len(kpis)}")
    rich = any(k.get(key) for k in kpis for key in TILE_KEYS) or         any(k.get("type") == "kpi_tile" for k in kpis)
    row_h = KPI_TILE_H if rich else KPI_H
    out = []
    cards_w = width - (280 + GUTTER if slicer else 0)
    n = len(kpis)
    w = (cards_w - (n - 1) * GUTTER) / n
    for i, k in enumerate(kpis):
        k = dict(k)
        x = x0 + i * (w + GUTTER)
        if rich:
            k["type"] = "kpi_tile"
            k.setdefault("index", i)            # tile color = tile_colors[i]
            out.append(_place(k, x, y, w, row_h))
            continue
        icon = k.pop("icon", None)
        k.setdefault("type", "kpi_card")
        out.append(_place(k, x, y, w, row_h))
        if icon:
            out.append({"type": "icon", "glyph": icon, "size": ICON_SIZE,
                        "color": _accent(style),
                        "x": int(x + w - ICON_SIZE - 16), "y": y + 16})
    if slicer:
        s = dict(slicer)
        s.setdefault("type", "slicer")
        out.append(_place(s, x0 + width - 280, y, 280, row_h))
    return out, row_h


def executive_hero(style: str, table: str, header: dict, kpis: list, hero: dict,
                   supporting: list = None, detail: list = None,
                   slicer: dict = None, page=0, nav: bool = False) -> dict:
    """
    The flagship layout — clear visual hierarchy, hero visual dominates.

      [ header band: title / subtitle          (page navigator) ]
      [ KPI ] [ KPI ] [ KPI ]                        [ slicer  ]
      [ HERO  60% wide, tall     ] [ supporting 1              ]
      [                          ] [ supporting 2              ]
      [ detail 1     ] [ detail 2      ] [ detail 3            ]

    kpis:       1-4 dicts {"title", "measure", "icon"(opt), ...}  (table auto-filled)
    hero:       one visual dict (no x/y/w/h) — the page's focal chart
    supporting: 0-2 visuals stacked right of the hero
    detail:     0-3 visuals in a bottom row
    slicer:     optional {"column", ...} — right end of the KPI row
    nav:        add a page navigator in the header zone (multi-page reports)
    """
    supporting = supporting or []
    detail = detail or []
    if len(supporting) > 2:
        raise ValueError(f"supporting: expected 0-2 visuals, got {len(supporting)}")
    if len(detail) > 3:
        raise ValueError(f"detail: expected 0-3 visuals, got {len(detail)}")

    visuals = []
    for v in [hero] + supporting + detail:
        v.setdefault("table", table)
    for k in kpis:
        k.setdefault("table", table)
    if slicer:
        slicer.setdefault("table", table)

    kpi_visuals, kpi_h = _kpi_row(kpis, slicer, style, CONTENT_Y)
    visuals += kpi_visuals

    # Hero row — hero takes ~60%, supporting stack right
    y1 = CONTENT_Y + kpi_h + GUTTER
    hero_h = 440
    hero_w = 740
    visuals.append(_place(hero, MARGIN, y1, hero_w, hero_h))
    if supporting:
        sx = MARGIN + hero_w + GUTTER
        sw = CANVAS_W - MARGIN - sx
        sh = (hero_h - (len(supporting) - 1) * GUTTER) / len(supporting)
        for i, v in enumerate(supporting):
            visuals.append(_place(v, sx, y1 + i * (sh + GUTTER), sw, sh))

    # Detail strip
    y2 = y1 + hero_h + GUTTER
    if detail:
        visuals += _row(detail, y2, 340)
        canvas_h = y2 + 340 + MARGIN
    else:
        canvas_h = y2 + MARGIN - GUTTER

    if nav:
        visuals.append({"type": "page_navigator",
                        "x": CANVAS_W - MARGIN - 320, "y": 25, "w": 320, "h": 40})

    return {
        "style": style, "theme": True,
        "pages": [{
            "page": page, "clear": True,
            "canvas": {"width": CANVAS_W, "height": int(canvas_h)},
            "background": {"title": header.get("title"),
                           "subtitle": header.get("subtitle")},
            "visuals": visuals,
        }],
    }


def sidebar_nav(style: str, table: str, header: dict, kpis: list, charts: list,
                slicer: dict = None, rail_icons: list = None,
                page=0, nav: bool = True) -> dict:
    """
    Left-rail layout for multi-page reports.

      [rail] [ header band: title / subtitle    (page navigator) ]
      [icon] [ KPI ] [ KPI ] [ KPI ]                  [ slicer  ]
      [icon] [ chart 1              ] [ chart 2                 ]
      [icon] [ chart 3              ] [ chart 4                 ]

    charts:     1-4 visuals in a 2-column grid
    rail_icons: up to 5 glyph names drawn down the rail (decorative)
    nav:        page navigator in the header (default ON — this layout is
                built for multi-page reports)
    """
    if not 1 <= len(charts) <= 4:
        raise ValueError(f"charts: expected 1-4 visuals, got {len(charts)}")
    rail_w = 64
    x0 = rail_w + GUTTER
    width = CANVAS_W - x0 - MARGIN

    for v in charts:
        v.setdefault("table", table)
    for k in kpis:
        k.setdefault("table", table)
    if slicer:
        slicer.setdefault("table", table)

    visuals, kpi_h = _kpi_row(kpis, slicer, style, CONTENT_Y, x0=x0, width=width)

    y1 = CONTENT_Y + kpi_h + GUTTER
    ch = 330
    cw = (width - GUTTER) / 2
    for i, v in enumerate(charts):
        col, row_i = i % 2, i // 2
        visuals.append(_place(v, x0 + col * (cw + GUTTER),
                              y1 + row_i * (ch + GUTTER), cw, ch))
    rows = (len(charts) + 1) // 2
    canvas_h = y1 + rows * (ch + GUTTER) + MARGIN - GUTTER

    for i, g in enumerate(rail_icons or []):
        visuals.append({"type": "icon", "glyph": g, "size": ICON_SIZE,
                        "color": _accent(style),
                        "x": (rail_w - ICON_SIZE) // 2,
                        "y": HEADER_H + 30 + i * 56})
    if nav:
        visuals.append({"type": "page_navigator",
                        "x": CANVAS_W - MARGIN - 320, "y": 25, "w": 320, "h": 40})

    return {
        "style": style, "theme": True,
        "pages": [{
            "page": page, "clear": True,
            "canvas": {"width": CANVAS_W, "height": int(canvas_h)},
            "background": {"title": header.get("title"),
                           "subtitle": header.get("subtitle"),
                           "nav_rail": rail_w},
            "visuals": visuals,
        }],
    }


def story_page(style: str, table: str, header: dict, hero_kpi: dict,
               narrative: str, charts: list, page=0, nav: bool = False) -> dict:
    """
    Data-storytelling page — one big number, its story, and the evidence.

      [ header band: title / subtitle                            ]
      [ HERO KPI (big)      ] [ narrative text                   ]
      [ evidence chart 1            ] [ evidence chart 2         ]

    hero_kpi:  one KPI dict {"title", "measure", "icon"(opt)}
    narrative: the story text (2-4 sentences; \\n for paragraphs)
    charts:    1-2 evidence visuals
    """
    if not 1 <= len(charts) <= 2:
        raise ValueError(f"charts: expected 1-2 visuals, got {len(charts)}")
    for v in charts:
        v.setdefault("table", table)
    hero_kpi = dict(hero_kpi)
    hero_kpi.setdefault("table", table)
    hero_kpi.setdefault("type", "kpi_card")
    icon = hero_kpi.pop("icon", None)

    hero_h = 220
    visuals = [_place(hero_kpi, MARGIN, CONTENT_Y, 500, hero_h)]
    if icon:
        visuals.append({"type": "icon", "glyph": icon, "size": 36,
                        "color": _accent(style),
                        "x": MARGIN + 500 - 36 - 20, "y": CONTENT_Y + 20})
    visuals.append({"type": "text_box", "text": narrative,
                    "font_size": 14, "bold": False,
                    "x": MARGIN + 500 + GUTTER, "y": CONTENT_Y + 20,
                    "w": CANVAS_W - MARGIN * 2 - 500 - GUTTER, "h": hero_h - 40})

    y1 = CONTENT_Y + hero_h + GUTTER
    visuals += _row(charts, y1, 380)
    canvas_h = y1 + 380 + MARGIN

    if nav:
        visuals.append({"type": "page_navigator",
                        "x": CANVAS_W - MARGIN - 320, "y": 25, "w": 320, "h": 40})

    return {
        "style": style, "theme": True,
        "pages": [{
            "page": page, "clear": True,
            "canvas": {"width": CANVAS_W, "height": int(canvas_h)},
            "background": {"title": header.get("title"),
                           "subtitle": header.get("subtitle")},
            "visuals": visuals,
        }],
    }


RAIL_W      = 220     # app_shell menu width (200 truncated "Source Effectiveness" at 11pt)
MENU_Y      = 92      # first menu button (under the brand block + divider)
MENU_BTN_H  = 40
MENU_GAP    = 5       # Power BI page navigator default grid padding
MENU_INDENT = 40      # button text indent; the page icon sits in that gap
MENU_ICON_X = 32


def app_shell(style: str, table: str, header: dict, kpis: list, hero: dict,
              pages: list, page_index: int = 0, brand: dict = None,
              supporting: list = None, detail: list = None,
              controls: list = None, clear_button: bool = True,
              footer_measure: str = None, footer_note: str = None,
              page=0) -> dict:
    """
    App-style multi-page layout — the frame the top-rated marketplace
    dashboards use: a labeled left menu and filters in the header.

      [ logo  Brand     ] [ header: title / subtitle   [control] [Clear] ]
      [ ─────────────── ] [ KPI tile ] [ KPI tile ] [ KPI tile ] [ ... ] ]
      [ ▣ Overview   ◀  ] [ HERO 60%                 ] [ supporting 1    ]
      [ ▣ Sources       ] [                          ] [ supporting 2    ]
      [ ▣ Speed         ] [ detail 1  ] [ detail 2   ] [ detail 3        ]
      [ Data through …  ]
      [ source note     ]

    pages:      EVERY report page, in report order: [("Overview", "home"), ...]
                — the menu (a vertical page navigator) lists them all; the
                glyphs are baked into the background beside each button
    page_index: which entry of `pages` this spec builds (its icon is lit;
                the navigator itself marks the current page as selected)
    brand:      {"name": "Hiring Analytics", "tagline": "...", "glyph": "people"}
    controls:   header filters, right-aligned: ordinary slicer dicts plus
                optional "w"/"h" (e.g. {"column": "seniority",
                "slicer_type": "tiles", "show_header": False, "w": 300})
    clear_button: a "Clear filters" button at the right end of the header
    footer_measure: text measure for the menu footer, e.g. the name returned
                by design_measures.add_data_through()
    footer_note: static text under it ("Source: ATS export")
    kpis/hero/supporting/detail: as executive_hero (KPIs span the full
                content width — filters live in the header, not the KPI row)
    """
    supporting = supporting or []
    detail = detail or []
    if len(supporting) > 2 or len(detail) > 3:
        raise ValueError("supporting: 0-2 visuals, detail: 0-3 visuals")
    if not 0 <= page_index < len(pages):
        raise ValueError(f"page_index {page_index} outside pages (len {len(pages)})")
    brand = brand or {}
    x0 = RAIL_W + GUTTER
    width = CANVAS_W - x0 - MARGIN

    for v in [hero] + supporting + detail:
        v.setdefault("table", table)
    for k in kpis:
        k.setdefault("table", table)

    visuals, kpi_h = _kpi_row(kpis, None, style, CONTENT_Y, x0=x0, width=width)

    y1 = CONTENT_Y + kpi_h + GUTTER
    hero_h = 440
    hero_w = round((width - GUTTER) * 0.6)
    visuals.append(_place(hero, x0, y1, hero_w, hero_h))
    if supporting:
        sx = x0 + hero_w + GUTTER
        sw = CANVAS_W - MARGIN - sx
        sh = (hero_h - (len(supporting) - 1) * GUTTER) / len(supporting)
        for i, v in enumerate(supporting):
            visuals.append(_place(v, sx, y1 + i * (sh + GUTTER), sw, sh))
    y2 = y1 + hero_h + GUTTER
    if detail:
        visuals += _row(detail, y2, 340, x0=x0, width=width)
        content_bottom = y2 + 340
    else:
        content_bottom = y2 - GUTTER
    footer_h = (30 if footer_measure else 0) + (24 if footer_note else 0)
    canvas_h = max(content_bottom + MARGIN,
                   MENU_Y + len(pages) * (MENU_BTN_H + MENU_GAP) + footer_h + 40)

    # header controls, right-aligned; never get a card slot (slot: False)
    right = CANVAS_W - MARGIN
    if clear_button:
        visuals.append({"type": "clear_button", "x": right - 110,
                        "y": (HEADER_H - 32) // 2, "w": 110, "h": 32, "slot": False})
        right -= 110 + 12
    for c in reversed(controls or []):
        c = dict(c)
        c.setdefault("type", "slicer")
        c.setdefault("table", table)
        w = c.pop("w", 220)
        h = c.pop("h", 56)
        visuals.append(dict(c, x=int(right - w), y=int((HEADER_H - h) // 2),
                            w=int(w), h=int(h), slot=False))
        right -= w + 12
    reserve = CANVAS_W - MARGIN - right

    # the menu: vertical page navigator + icons baked into the background
    nav_h = len(pages) * MENU_BTN_H + (len(pages) - 1) * MENU_GAP
    visuals.append({"type": "page_navigator", "orientation": "vertical",
                    "text_indent": MENU_INDENT, "font_size": 11,
                    "x": 12, "y": MENU_Y, "w": RAIL_W - 24, "h": nav_h})
    items = [{"glyph": g, "y": MENU_Y + i * (MENU_BTN_H + MENU_GAP),
              "h": MENU_BTN_H, "active": i == page_index}
             for i, (_, g) in enumerate(pages)]
    fy = canvas_h - footer_h - 16
    if footer_measure:
        visuals.append({"type": "text_measure", "table": table,
                        "measure": footer_measure, "font_size": 9,
                        "x": 16, "y": fy, "w": RAIL_W - 32, "h": 26})
        fy += 30
    if footer_note:
        visuals.append({"type": "text_box", "text": footer_note, "font_size": 8,
                        "bold": False, "align": "left",
                        "color": STYLES.get(style, STYLES[DEFAULT_STYLE])["text_secondary"],
                        "x": 12, "y": fy, "w": RAIL_W - 24, "h": 24})

    return {
        "style": style, "theme": True,
        "pages": [{
            "page": page, "clear": True,
            "canvas": {"width": CANVAS_W, "height": int(canvas_h)},
            "background": {"title": header.get("title"),
                           "subtitle": header.get("subtitle"),
                           "reserve_right": int(reserve),
                           "nav_rail": {"width": RAIL_W,
                                        "brand": brand.get("name", ""),
                                        "tagline": brand.get("tagline"),
                                        "logo_glyph": brand.get("glyph", "chart"),
                                        "icon_x": MENU_ICON_X, "items": items}},
            "visuals": visuals,
        }],
    }


TEMPLATES = {
    "app_shell":      app_shell,
    "executive_hero": executive_hero,
    "sidebar_nav":    sidebar_nav,
    "story_page":     story_page,
}
