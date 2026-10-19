"""
brand_styles.py — a complete dashboard style + Power BI theme from ONE brand color.

    from brand_styles import create_brand_style
    create_brand_style("acme", "#E4572E", mode="dark")          # or mode="light"
    create_brand_style("acme_exec", "#1D4ED8", mode="light", header="filled")
    # then style="acme" everywhere (templates, add_*, spec "style")

What it produces
  * a STYLES entry (page/card/border/text colors, accent, muted, tile colors,
    data colors, positive/negative/neutral) — registered in pbir_editor.STYLES
    now and persisted to _data/brand_styles.json, so later sessions see it;
  * a theme JSON (<THEMES_DIR>/professional_<name>.json) cloned from the
    matching base theme with every UI color swapped — embed_theme(name) and
    build_from_spec(style=name) pick it up like the built-in presets.

Design rules baked in (contrast is checked, not hoped for):
  * page/card/border are the brand hue at low saturation — the dashboard
    "feels" like the brand without the brand color flooding the canvas;
  * the accent is the brand color, nudged lighter (dark mode) or darker
    (light mode) until it reaches 3:1 against the cards (WCAG non-text);
    with header="filled" it must also carry white header text at 4.5:1;
  * secondary text reaches 4.5:1 on the cards; muted bars stay visibly
    secondary (they are the "everything else" in a highlighted chart);
  * tile colors rotate around the brand hue, each carrying a white glyph.
"""

import colorsys
import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
STORE = os.path.join(HERE, "_data", "brand_styles.json")
THEMES_DIR = os.environ.get(
    "PBIMAKE_THEMES_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "themes"))
BASE_THEME = {"dark": "professional_dark_analytics.json",
              "light": "professional_corporate_light.json"}
# UI color of each base theme -> style key that replaces it
BASE_UI = {
    "dark": {"#1A2033": "page_bg", "#242B3D": "card_bg", "#2E3650": "secondary_bg",
             "#3A4260": "border", "#F0F0F0": "text_primary", "#A0A8BC": "text_secondary",
             "#00B4D8": "accent"},
    "light": {"#F3F2F1": "page_bg", "#FFFFFF": "card_bg", "#E1DFDD": "border",
              "#252423": "text_primary", "#605E5C": "text_secondary",
              "#0078D4": "accent", "#D1ECFF": "accent_tint"},
}


# ── color math ────────────────────────────────────────────────────────────────

def _rgb(hexc: str) -> tuple:
    h = hexc.lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))


def _hex(rgb: tuple) -> str:
    return "#" + "".join(f"{round(max(0, min(1, c)) * 255):02X}" for c in rgb)


def _hls(h: float, l: float, s: float) -> str:
    return _hex(colorsys.hls_to_rgb(h % 1.0, max(0, min(1, l)), max(0, min(1, s))))


def luminance(hexc: str) -> float:
    def ch(c):
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (ch(c) for c in _rgb(hexc))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: str, b: str) -> float:
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def _push(hexc: str, against: str, target: float, lighter: bool) -> str:
    """Move a color's lightness until it reaches `target` contrast."""
    h, l, s = colorsys.rgb_to_hls(*_rgb(hexc))
    for _ in range(60):
        c = _hls(h, l, s)
        if contrast(c, against) >= target:
            return c
        l = min(1, l + 0.015) if lighter else max(0, l - 0.015)
    return _hls(h, l, s)


# ── style derivation ──────────────────────────────────────────────────────────

def derive_style(brand_hex: str, mode: str = "dark", header: str = "band") -> dict:
    """STYLES-compatible dict from one brand color. mode: dark | light;
    header: band (card-colored header) | filled (brand-colored header)."""
    if mode not in ("dark", "light"):
        raise ValueError("mode must be 'dark' or 'light'")
    if header not in ("band", "filled"):
        raise ValueError("header must be 'band' or 'filled'")
    if not re.fullmatch(r"#[0-9A-Fa-f]{6}", brand_hex):
        raise ValueError(f"brand_hex must look like #1D4ED8, got {brand_hex!r}")
    h, l, s = colorsys.rgb_to_hls(*_rgb(brand_hex))
    if mode == "dark":
        cs = min(s, 0.45)
        st = {
            "page_bg": _hls(h, 0.10, cs * 0.6), "card_bg": _hls(h, 0.15, cs * 0.55),
            "secondary_bg": _hls(h, 0.19, cs * 0.5), "border": _hls(h, 0.27, cs * 0.5),
            "text_primary": "#F2F4F8",
        }
        card = st["card_bg"]
        st["text_secondary"] = _push(_hls(h, 0.70, 0.15), card, 4.5, lighter=True)
        st["accent"] = _push(brand_hex.upper(), card, 3.0, lighter=True)
        st["muted"] = _hls(h, 0.36, 0.18)
        tile_l, tile_s = 0.56, 0.68
        st["positive"] = _push("#22C55E", card, 3.0, lighter=True)
        st["negative"] = _push("#F43F5E", card, 3.0, lighter=True)
        st["neutral"] = "#F59E0B"
    else:
        cs = min(s, 0.30)
        st = {
            "page_bg": _hls(h, 0.965, cs), "card_bg": "#FFFFFF",
            "secondary_bg": _hls(h, 0.94, cs), "border": _hls(h, 0.88, cs * 0.7),
            "text_primary": "#1F2328",
        }
        card = st["card_bg"]
        st["text_secondary"] = _push(_hls(h, 0.42, 0.12), card, 4.5, lighter=False)
        st["accent"] = _push(brand_hex.upper(), card, 3.0, lighter=False)
        st["muted"] = _hls(h, 0.84, 0.18)
        tile_l, tile_s = 0.46, 0.65
        st["positive"] = _push("#15803D", card, 3.0, lighter=False)
        st["negative"] = _push("#DC2626", card, 3.0, lighter=False)
        st["neutral"] = "#D97706"
    if header == "filled":
        # white header text must read on the brand color
        st["accent"] = _push(st["accent"], "#FFFFFF", 4.5, lighter=False)
        st["header_fill"] = True
    ah, al, as_ = colorsys.rgb_to_hls(*_rgb(st["accent"]))
    st["accent_tint"] = _hls(ah, 0.90 if mode == "light" else 0.25, as_ * 0.6)
    rot = [0.0, 0.13, 0.5, 0.75, 0.33, 0.9]

    def _tile_hue(x):
        # yellow/lime darkened to carry a white glyph turns olive — use amber
        x %= 1.0
        return 0.075 if 0.10 <= x <= 0.24 else x
    tiles = [st["accent"]] + [_hls(_tile_hue(h + r), tile_l, tile_s) for r in rot[1:]]
    # white glyphs on every tile
    st["tile_colors"] = [_push(t, "#FFFFFF", 3.0, lighter=False) for t in tiles]
    st["data_colors"] = [st["accent"], _hls(ah, 0.72 if mode == "dark" else 0.62, as_),
                         tiles[2], tiles[3], tiles[1], st["neutral"], st["positive"],
                         tiles[4]]
    st["brand"] = brand_hex.upper()
    st["mode"] = mode
    return st


def _mix(a: str, b: str, t: float) -> str:
    """Blend two colors in sRGB. t=0 is a, t=1 is b. Good enough for UI tints,
    which is all it is used for."""
    ra, rb = _rgb(a), _rgb(b)
    return _hex(tuple(ra[i] + (rb[i] - ra[i]) * t for i in range(3)))


# ── SVG colour tokens ─────────────────────────────────────────────────────────
# The palette an SVG-measure report draws from. Each token becomes one
# `Color <token>` DAX measure (svg_measures.add_theme_measures), and the SVG
# primitives name tokens rather than colours, so a Light/Dark slicer click
# restyles the whole report. One entry per token, in the order written out.
SVG_TOKENS = {
    "page":       "page canvas behind every card",
    "sidebar":    "nav rail / secondary surface",
    "card":       "card background",
    "cardalt":    "accented card background (the one card that stands out)",
    "tile":       "inner tile inside a card",
    "hairline":   "1px separator",
    "line":       "chart gridline / axis rule (lighter than hairline)",
    "ink":        "primary text",
    "muted":      "secondary text and axis labels",
    "accent":     "the highlighted mark — newest bar, gauge fill, KPI arc",
    "accentsoft": "the same series de-emphasised (every other bar)",
    "chip":       "icon chip behind a glyph",
    "chipicon":   "glyph stroke on the chip",
    "track":      "accent-tinted track a progress or gauge mark runs along",
    "track2":     "neutral track, for the secondary series",
    "positive":   "up / good",
    "negative":   "down / bad",
    "neutral":    "flat / warning",
    "halo":       "ring drawn behind a mark to lift it off the card",
    "series2":    "second data colour, for a comparison series",
}


def svg_tokens(style: dict) -> dict:
    """Derive the 20 SVG colour tokens from a STYLES dict.

    Works for the built-in presets as well as brand styles: the keys those
    lack (mode, secondary_bg, accent_tint) are inferred rather than required.
    """
    page = style["page_bg"]
    card = style["card_bg"]
    accent = style["accent"]
    dark = style.get("mode") == "dark" or (
        "mode" not in style and luminance(page) < 0.4)
    secondary = style.get("secondary_bg") or _mix(page, card, 0.5)
    tint = style.get("accent_tint") or _mix(accent, card, 0.85)
    border = style["border"]
    ink = style["text_primary"]
    muted = style["text_secondary"]

    chip = tint if not dark else _mix(accent, card, 0.76)
    tok = {
        "page": page,
        # the rail sits between page and card in both modes — that is what
        # reads as "behind the cards" rather than as another card
        "sidebar": _mix(page, card, 0.35),
        "card": card,
        "cardalt": _mix(card, tint, 0.55),
        # an inner tile has to be told apart from its card without becoming a
        # second surface: a nudge toward white in dark mode, toward the page
        # in light mode
        "tile": _mix(card, "#FFFFFF", 0.07) if dark else _mix(card, page, 0.5),
        "hairline": border,
        "line": _mix(border, card, 0.35),
        "ink": ink,
        "muted": muted,
        "accent": accent,
        # a de-emphasised bar must still read as a bar, but it must also read
        # as NOT the highlighted one: blended most of the way to the card, then
        # floored at 1.4:1 against it
        "accentsoft": _push(_mix(accent, card, 0.70 if dark else 0.58), card,
                            1.4, lighter=dark),
        "chip": chip,
        # the glyph has to survive on the chip, whatever the chip turned out to be
        "chipicon": _push(accent if not dark else "#F7F3EA", chip, 3.0,
                          lighter=dark),
        "track": _mix(accent, card, 0.84),
        "track2": _mix(muted, card, 0.80),
        "positive": style["positive"],
        "negative": style["negative"],
        "neutral": style["neutral"],
        "halo": card,
        "series2": (style.get("data_colors") or [accent, accent])[1],
    }
    missing = [k for k in SVG_TOKENS if k not in tok]
    if missing:
        raise AssertionError("svg_tokens is missing %s" % missing)
    return {k: tok[k] for k in SVG_TOKENS}


def derive_theme_pair(brand_hex: str, header: str = "band") -> dict:
    """{"light": {...20 tokens}, "dark": {...}} from one brand colour — the
    pair an SVG-measure report switches between with its Theme slicer."""
    return {mode: svg_tokens(derive_style(brand_hex, mode, header))
            for mode in ("light", "dark")}


def token_contrast(tokens: dict) -> dict:
    """The ratios a token palette promises, for tests and review."""
    card = tokens["card"]
    return {k: round(v, 2) for k, v in {
        "ink_vs_card": contrast(tokens["ink"], card),
        "muted_vs_card": contrast(tokens["muted"], card),
        "accent_vs_card": contrast(tokens["accent"], card),
        "accentsoft_vs_card": contrast(tokens["accentsoft"], card),
        "chipicon_vs_chip": contrast(tokens["chipicon"], tokens["chip"]),
        "card_vs_page": contrast(card, tokens["page"]),
        "positive_vs_card": contrast(tokens["positive"], card),
        "negative_vs_card": contrast(tokens["negative"], card),
    }.items()}


def build_theme(name: str, style: dict) -> dict:
    """Clone the base theme for the style's mode and swap every UI color."""
    mode = style["mode"]
    with open(os.path.join(THEMES_DIR, BASE_THEME[mode]), encoding="utf-8") as f:
        text = f.read()
    # one pass, so a color inserted early can never be swapped again later
    ui = {old.upper(): style[key] for old, key in BASE_UI[mode].items() if key in style}
    pattern = re.compile("|".join(re.escape(k) for k in ui), flags=re.IGNORECASE)
    text = pattern.sub(lambda m: ui[m.group(0).upper()], text)
    t = json.loads(text)
    t["name"] = f"Brand - {name}"
    t["dataColors"] = style["data_colors"]
    t["good"], t["bad"], t["neutral"] = style["positive"], style["negative"], style["neutral"]
    return t


def create_brand_style(name: str, brand_hex: str, mode: str = "dark",
                       header: str = "band", overwrite: bool = True) -> dict:
    """Derive, register (pbir_editor.STYLES) and persist a brand style, and
    write its theme JSON. Returns the style dict."""
    import pbir_editor
    if not re.fullmatch(r"[a-z][a-z0-9_]{1,40}", name):
        raise ValueError("name: lowercase letters, digits, underscores (e.g. 'acme_dark')")
    builtin = {"corporate_light", "dark_analytics", "minimal_neutral"}
    if name in builtin:
        raise ValueError(f"'{name}' is a built-in preset — pick another name")
    style = derive_style(brand_hex, mode, header)
    theme_path = os.path.join(THEMES_DIR, f"professional_{name}.json")
    if os.path.exists(theme_path) and not overwrite:
        raise FileExistsError(theme_path)
    with open(theme_path, "w", encoding="utf-8") as f:
        json.dump(build_theme(name, style), f, indent=2, ensure_ascii=False)
    store = load_store()
    store[name] = style
    os.makedirs(os.path.dirname(STORE), exist_ok=True)
    with open(STORE, "w", encoding="utf-8") as f:
        json.dump(store, f, indent=2)
    pbir_editor.STYLES[name] = style
    print(f"  brand style '{name}' ({mode}, header={header}) -> {theme_path}")
    return style


def load_store() -> dict:
    if not os.path.exists(STORE):
        return {}
    with open(STORE, encoding="utf-8") as f:
        return json.load(f)


def contrast_report(style: dict) -> dict:
    """The ratios the derivation promises — for tests and the retrospective."""
    card = style["card_bg"]
    rep = {
        "accent_vs_card": contrast(style["accent"], card),
        "text_secondary_vs_card": contrast(style["text_secondary"], card),
        "text_primary_vs_card": contrast(style["text_primary"], card),
        "min_tile_vs_white": min(contrast(t, "#FFFFFF") for t in style["tile_colors"]),
        "positive_vs_card": contrast(style["positive"], card),
        "negative_vs_card": contrast(style["negative"], card),
    }
    if style.get("header_fill"):
        rep["white_on_header"] = contrast("#FFFFFF", style["accent"])
    return {k: round(v, 2) for k, v in rep.items()}
