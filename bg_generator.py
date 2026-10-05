"""
bg_generator.py — programmatic Power BI page-background generator (Pillow).

Competition-winning Power BI dashboards use a designed background image
(traditionally drawn in Figma/PowerPoint): the page color with pre-drawn
"card slots" — rounded rectangles with soft shadows — plus a header band
and an optional nav rail. Visuals then sit on top with transparent
backgrounds, perfectly aligned to their slots.

This module generates that image from the same grid the visuals use, so
alignment is pixel-perfect by construction. No Figma needed.

Used by pbir_editor.PbirPage.generate_background(); usable standalone:

    from bg_generator import generate_background
    generate_background(
        "bg.png", 1280, 720,
        colors=STYLES["corporate_light"],       # needs page_bg/card_bg/accent/...
        slots=[{"x": 20, "y": 110, "w": 390, "h": 120, "accent": "left"}, ...],
        header={"title": "Sales Performance", "subtitle": "FY26 Q2", "height": 90},
        nav_rail={"width": 56},
    )

Rendering is supersampled (draw at 2x, downsample LANCZOS) for smooth
rounded corners and shadows.
"""

import os
import sys
from PIL import Image, ImageDraw, ImageFilter, ImageFont

# Header text metrics (px at 1x scale)
TITLE_PX    = 24
SUBTITLE_PX = 12
PAD_X       = 28   # header text left padding (after nav rail)
ACCENT_STRIPE_H = 4  # brand stripe along the very top


ICON_TILE_PX  = 36   # KPI tile icon square (matches pbir_editor.add_kpi_tile)
ICON_TILE_PAD = 14


_GLYPHS_WARNED = False


def _glyph_font_or_none(px: int):
    """Segoe MDL2 Assets at px, or None when the font is not installed.

    icon_generator._glyph_font deliberately refuses to substitute another face,
    because a substitute renders tofu boxes rather than icons. This is the
    background's equivalent of that refusal: drop the glyph, say so once on
    stderr, and leave every piece of geometry around it untouched — so the
    layout a Windows render would produce is identical except for the glyphs.
    """
    global _GLYPHS_WARNED
    from icon_generator import _glyph_font
    try:
        return _glyph_font(px)
    except FileNotFoundError as ex:
        if not _GLYPHS_WARNED:
            _GLYPHS_WARNED = True
            print(f"  [warn] background drawn WITHOUT glyph icons: {ex}",
                  file=sys.stderr)
        return None


def _icon_tile(draw, x: int, y: int, size: int, glyph: str, color: str) -> None:
    """Rounded square in the KPI's color with its glyph knocked out in white."""
    from icon_generator import GLYPHS
    draw.rounded_rectangle((x, y, x + size, y + size), radius=int(size * 0.26),
                           fill=color)
    font = _glyph_font_or_none(int(size * 0.5))
    if font is None:
        return
    draw.text((x + size / 2, y + size / 2), GLYPHS.get(glyph, glyph), font=font,
              fill=(255, 255, 255, 255), anchor="mm")


def _hex_rgb(color: str) -> tuple:
    c = color.lstrip("#")
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))

def _is_dark(color: str) -> bool:
    r, g, b = _hex_rgb(color)
    return (0.299 * r + 0.587 * g + 0.114 * b) < 128

def _font(px: int, semibold: bool = False):
    """Segoe UI from Windows fonts; graceful fallback to Pillow default."""
    names = ("seguisb.ttf", "segoeuib.ttf") if semibold else ("segoeui.ttf",)
    fonts_dir = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
    for name in names:
        p = os.path.join(fonts_dir, name)
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, px)
            except OSError:
                continue
    return ImageFont.load_default(px)


def _fit(draw, text: str, font, max_w: float) -> str:
    """Ellipsize text to fit max_w pixels (long headers must not overflow the band)."""
    text = str(text)
    if draw.textlength(text, font=font) <= max_w:
        return text
    while text and draw.textlength(text + "…", font=font) > max_w:
        text = text[:-1]
    return text.rstrip() + "…"


def generate_background(out_path: str, width: int, height: int, colors: dict,
                        slots: list = None, header: dict = None,
                        nav_rail: dict = None, radius: int = 10,
                        shadow: bool = True, border: bool = False,
                        scale: int = 2) -> str:
    """
    Draw a dashboard background PNG and return out_path.

    width/height : canvas size in Power BI px (image is 1:1 with the canvas)
    colors       : style dict — requires page_bg, card_bg, accent,
                   text_primary, text_secondary, border
    slots        : card rectangles; (x, y, w, h) tuples or dicts
                   {"x","y","w","h","accent": "left"|None}
    header       : {"title": str|None, "subtitle": str|None, "height": 90}
                   draws a full-width band with baked text + accent stripe
    nav_rail     : {"width": 56} draws a left rail with a logo placeholder
    radius       : card corner radius (px at 1x)
    shadow       : soft drop shadow under each card (skip for minimal styles)
    border       : 1px card outline (use INSTEAD of shadow for minimal styles)
    scale        : supersampling factor (2 = draw at 2x, downsample)
    """
    s = scale
    W, H = width * s, height * s
    page_bg  = colors["page_bg"]
    card_bg  = colors["card_bg"]
    accent   = colors["accent"]
    border_c = colors["border"]
    dark     = _is_dark(page_bg)

    img  = Image.new("RGBA", (W, H), page_bg)
    draw = ImageDraw.Draw(img)

    rail_w = (nav_rail or {}).get("width", 0) * s
    band_h = (header or {}).get("height", 0) * s if header else 0

    # ── Nav rail (left) ─────────────────────────────────────────────────────────
    if rail_w:
        draw.rectangle((0, 0, rail_w, H), fill=card_bg)
        draw.rectangle((rail_w - s, 0, rail_w, H), fill=border_c)      # hairline
        brand = nav_rail.get("brand")
        if brand:
            # labeled app menu: logo tile + brand/tagline, divider, page icons
            _icon_tile(draw, 18 * s, 22 * s, 36 * s,
                       nav_rail.get("logo_glyph", "chart"), accent)
            bx = 64 * s
            bw = rail_w - bx - 10 * s
            bfont = _font(15 * s, semibold=True)
            draw.text((bx, 33 * s), _fit(draw, brand, bfont, bw),
                      font=bfont, fill=colors["text_primary"], anchor="lm")
            if nav_rail.get("tagline"):
                tfont = _font(10 * s)
                draw.text((bx, 52 * s), _fit(draw, nav_rail["tagline"], tfont, bw),
                          font=tfont, fill=colors["text_secondary"], anchor="lm")
            draw.rectangle((16 * s, 76 * s, rail_w - 16 * s, 77 * s), fill=border_c)
            from icon_generator import GLYPHS
            gfont = _glyph_font_or_none(16 * s)
            ix = nav_rail.get("icon_x", 30) * s
            for it in (nav_rail.get("items", []) if gfont else []):
                cy = (it["y"] + it["h"] / 2) * s
                col = colors["text_primary"] if it.get("active") else colors["text_secondary"]
                draw.text((ix, cy), GLYPHS.get(it["glyph"], it["glyph"]), font=gfont,
                          fill=col, anchor="mm")
        else:
            logo = 28 * s                                              # logo placeholder
            lx = (rail_w - logo) // 2
            draw.rounded_rectangle((lx, 20 * s, lx + logo, 20 * s + logo),
                                   radius=8 * s, fill=accent)

    # ── Header band ─────────────────────────────────────────────────────────────
    filled = bool(colors.get("header_fill"))
    if band_h:
        # "filled" brand styles paint the band in the accent with white text
        # (brand_styles guarantees 4.5:1); the default band is card-colored
        draw.rectangle((rail_w, 0, W, band_h), fill=accent if filled else card_bg)
        if not filled:
            draw.rectangle((rail_w, band_h - s, W, band_h), fill=border_c)  # hairline
        tx = rail_w + PAD_X * s
        # right padding for the band text (+ room for header controls, if any)
        text_w = W - tx - (40 + header.get("reserve_right", 0)) * s
        title    = header.get("title")
        subtitle = header.get("subtitle")
        title_font, sub_font = _font(TITLE_PX * s, semibold=True), _font(SUBTITLE_PX * s)
        t_col = "#FFFFFF" if filled else colors["text_primary"]
        s_col = "#E6EAF2" if filled else colors["text_secondary"]
        if title and subtitle:
            draw.text((tx, band_h * 0.38), _fit(draw, title, title_font, text_w),
                      font=title_font, fill=t_col, anchor="lm")
            draw.text((tx, band_h * 0.72), _fit(draw, subtitle, sub_font, text_w),
                      font=sub_font, fill=s_col, anchor="lm")
        elif title:
            draw.text((tx, band_h * 0.52), _fit(draw, title, title_font, text_w),
                      font=title_font, fill=t_col, anchor="lm")
        elif subtitle:
            draw.text((tx, band_h * 0.52), _fit(draw, subtitle, sub_font, text_w),
                      font=sub_font, fill=s_col, anchor="lm")

    # Brand stripe across the very top (over rail + band)
    if (band_h or rail_w) and not filled:
        draw.rectangle((0, 0, W, ACCENT_STRIPE_H * s), fill=accent)

    # ── Normalize slots ─────────────────────────────────────────────────────────
    norm, icons = [], []
    for sl in (slots or []):
        if isinstance(sl, dict):
            norm.append((sl["x"] * s, sl["y"] * s, sl["w"] * s, sl["h"] * s,
                         sl.get("accent")))
            if sl.get("icon"):
                icons.append((sl["x"] * s, sl["y"] * s, sl["icon"]))
        else:
            x, y, w, h = sl
            norm.append((x * s, y * s, w * s, h * s, None))

    # ── Shadows (one blurred layer under all cards) ─────────────────────────────
    if shadow and norm:
        sh_alpha = 90 if dark else 34
        layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        sh = ImageDraw.Draw(layer)
        for x, y, w, h, _ in norm:
            sh.rounded_rectangle((x, y + 3 * s, x + w, y + h + 3 * s),
                                 radius=radius * s, fill=(0, 0, 0, sh_alpha))
        layer = layer.filter(ImageFilter.GaussianBlur(4 * s))
        img = Image.alpha_composite(img, layer)
        draw = ImageDraw.Draw(img)

    # ── Card slots ──────────────────────────────────────────────────────────────
    for x, y, w, h, slot_accent in norm:
        draw.rounded_rectangle((x, y, x + w, y + h), radius=radius * s,
                               fill=card_bg,
                               outline=border_c if border else None,
                               width=s if border else 0)
        if slot_accent == "left":
            inset = 12 * s
            draw.rounded_rectangle((x, y + inset, x + 4 * s, y + h - inset),
                                   radius=2 * s, fill=accent)

    # ── Icon tiles (KPI tiles): colored rounded square + white glyph ────────────
    for x, y, ic in icons:
        _icon_tile(draw, x + ICON_TILE_PAD * s, y + ICON_TILE_PAD * s,
                   ICON_TILE_PX * s, ic["glyph"], ic.get("color") or accent)

    # ── Downsample + save ───────────────────────────────────────────────────────
    out = img.resize((width, height), Image.LANCZOS).convert("RGB")
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    out.save(out_path, optimize=True)
    return out_path
