"""
icon_generator.py — crisp UI icons from the Segoe MDL2 Assets glyph font.

Segoe MDL2 Assets (C:\\Windows\\Fonts\\segmdl2.ttf) ships with Windows 10/11 —
professional icons with zero external assets. Rendered to transparent PNGs
(supersampled 4x) for use as Power BI image visuals.

Used by pbir_editor.PbirPage.add_icon(); usable standalone.

GLYPH CODES: verified visually 2026-07-02 (contact sheet). Add new ones from
https://learn.microsoft.com/en-us/windows/apps/design/style/segoe-ui-symbol-font
and re-verify — codes are NOT guessable.
"""

import os
from PIL import Image, ImageDraw, ImageFont

FONT_PATH = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts", "segmdl2.ttf")

# name -> glyph char (codes verified visually via contact_sheet)
GLYPHS = {
    "people":    chr(0xE716),
    "person":    chr(0xE77B),
    "home":      chr(0xE80F),
    "calendar":  chr(0xE787),
    "mail":      chr(0xE715),
    "settings":  chr(0xE713),
    "filter":    chr(0xE71C),  # funnel shape
    "globe":     chr(0xE774),
    "shop":      chr(0xE719),
    "cart":      chr(0xE7BF),
    "star":      chr(0xE734),
    "lightning": chr(0xE945),
    "chart":     chr(0xE9D2),
    "money":     chr(0xE825),
    "tag":       chr(0xE8EC),
    "clock":     chr(0xE823),
}


def _hex_rgb(color: str) -> tuple:
    c = color.lstrip("#")
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))


def _glyph_font(px: int):
    """Segoe MDL2 Assets, or a clear error — a substitute font would silently
    render tofu boxes instead of icons."""
    try:
        return ImageFont.truetype(FONT_PATH, px)
    except OSError as ex:
        raise FileNotFoundError(
            f"Segoe MDL2 Assets not found at {FONT_PATH} — glyph icons require "
            "Windows 10/11. Use add_image() with your own PNG instead."
        ) from ex


def generate_icon(glyph: str, color: str, out_dir: str,
                  size: int = 32, scale: int = 4) -> str:
    """
    Render one glyph to a transparent PNG. Returns the file path.

    glyph: a GLYPHS name ("people") or a literal glyph character
    color: hex fill color
    size:  output PNG size in px (square)
    """
    ch = GLYPHS.get(glyph, glyph)
    name = glyph if glyph in GLYPHS else f"u{ord(ch[0]):04X}"
    fname = f"icon_{name}_{color.lstrip('#').lower()}_{size}.png"
    out_path = os.path.join(out_dir, fname)

    s = size * scale
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    font = _glyph_font(int(s * 0.92))
    draw.text((s // 2, s // 2), ch, font=font,
              fill=_hex_rgb(color) + (255,), anchor="mm")
    img = img.resize((size, size), Image.LANCZOS)
    os.makedirs(out_dir, exist_ok=True)
    img.save(out_path)
    return out_path


def contact_sheet(out_path: str, codes: list = None, color: str = "#00B4D8",
                  cell: int = 96) -> str:
    """Labeled grid of glyphs for visual verification of codes."""
    items = codes or [(n, c) for n, c in GLYPHS.items()]
    cols = 4
    rows = (len(items) + cols - 1) // cols
    img = Image.new("RGB", (cols * cell * 2, rows * cell), "#1A2033")
    draw = ImageDraw.Draw(img)
    glyph_font = _glyph_font(int(cell * 0.55))
    try:
        label_font = ImageFont.truetype(
            os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts", "segoeui.ttf"), 16)
    except OSError:
        label_font = ImageFont.load_default()
    for i, (name, ch) in enumerate(items):
        cx = (i % cols) * cell * 2
        cy = (i // cols) * cell
        draw.text((cx + cell // 2, cy + cell // 2), ch, font=glyph_font,
                  fill=color, anchor="mm")
        draw.text((cx + cell + 8, cy + cell // 2),
                  f"{name}\nU+{ord(ch[0]):04X}", font=label_font,
                  fill="#F0F0F0", anchor="lm")
    img.save(out_path)
    return out_path
