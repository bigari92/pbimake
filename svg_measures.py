"""
svg_measures.py — compose DAX measures that RETURN AN SVG DOCUMENT as a data URI.

This is the technique behind high-end Power BI reports: no chart visual, no
custom visual. A measure concatenates an SVG document from live values and
returns it as "data:image/svg+xml;utf8,..."; a chrome-stripped cardVisual
renders it (pbir_editor.PbirPage.add_svg_card). Every colour is itself a
measure, so one slicer click re-themes the whole report.

What this module emits is DAX, not SVG. Python composes the geometry — where
the marks sit, how wide the bars are, which font size fits — and returns a DAX
string-concatenation expression that redraws that geometry from live values.

    from svg_measures import kpi_sparkbar, TOKENS_LIGHT, render_sample

    card = kpi_sparkbar(w=348, h=119, title="Flights",
                        value=("Flights this period", 1284),
                        change=("Flights change label", "+8.2%"),
                        series=..., tokens=TOKENS_LIGHT)
    dax, width, height = card                    # the primitive contract
    model.add_measure("_Measures", "KPI flights (SVG)", dax,
                      text_measure=True, data_category="ImageUrl")
    page.add_svg_card("_Measures", "KPI flights (SVG)", 238, 102, width, height)

WHY THE HARNESS MATTERS
A malformed SVG renders as a silently blank card: Power BI reports no error at
all. So every primitive carries its own sample values and can render itself
offline, with no DAX engine involved:

    render_sample(card, "out/kpi.svg")     # writes the file, asserts it is XML

The sample render and the live DAX come from ONE pass over the geometry: every
scalar in here is a dual-mode value carrying both its DAX expression and its
sample result (see Num / Txt). A geometry change cannot reach the DAX without
also reaching the rendered sample.

THREE RULES THE EMITTED DAX OBEYS
  1. "%" is escaped before "#". Reverse the order and the % introduced by %23
     is itself re-escaped into %2523, and the card goes blank.
  2. The <svg> width/height equal the host card's position.width/height
     exactly, over a slightly larger viewBox, with preserveAspectRatio='none'.
     That is what makes placement pixel-exact; _validate_svg_measures.py gates
     it.
  3. Numbers are written with FORMAT(x, "0.0", "en-US"). Without the explicit
     locale a comma decimal separator turns x='12,5' into invalid SVG.
"""

import math
import os
import re
import xml.etree.ElementTree as ET

from brand_styles import SVG_TOKENS, derive_style, svg_tokens

__version__ = "1.0"

# Default palettes, so a primitive can be rendered before anyone has picked a
# brand colour. Any STYLES entry or brand style gives a better pair —
# brand_styles.derive_theme_pair("#C89C50") or svg_tokens(STYLES["my_brand"]).
TOKENS_LIGHT = svg_tokens(derive_style("#2F6FEB", "light"))
TOKENS_DARK = svg_tokens(derive_style("#2F6FEB", "dark"))

# ── Font metrics ───────────────────────────────────────────────────────────────
# SVG has no layout engine: text does not wrap, does not shrink and does not
# clip — a long value simply draws past the edge of the card. Every primitive
# therefore applies an explicit text-fitting rule, computed from these measured
# advances (em per character, Segoe UI).
EM_DIGIT = 0.56     # digits at weight 600 — tabular, so all digits are equal
EM_LABEL = 0.52     # mixed-case label at weight 400, averaged over en text
EM_CAPS = 0.62      # upper-case label at weight 600

FONT = "Segoe UI, Arial, sans-serif"


# ── DAX literals ───────────────────────────────────────────────────────────────

def dax_str(s) -> str:
    """A DAX string literal. SVG attributes are single-quoted throughout, so
    the only character needing an escape here is the double quote itself."""
    return '"' + str(s).replace('"', '""') + '"'


def _half_up(x: float) -> float:
    return math.floor(x + 0.5) if x >= 0 else math.ceil(x - 0.5)


_FMT = {
    "0": (0, False), "0.0": (1, False), "0.00": (2, False),
    "#,0": (0, True), "#,0.0": (1, True), "#,0.00": (2, True),
}


def py_format(val, pattern: str) -> str:
    """Emulate the DAX FORMAT patterns this module emits, so the sample render
    and the live card agree digit for digit. An unknown pattern raises rather
    than diverge silently."""
    pct = pattern.endswith("%")
    core = pattern[:-1] if pct else pattern
    if core not in _FMT:
        raise ValueError(
            "py_format: unsupported FORMAT pattern %r. Supported: %s "
            "(optionally suffixed with a percent sign)" % (pattern, sorted(_FMT)))
    dec, group = _FMT[core]
    v = float(val) * (100 if pct else 1)
    scaled = _half_up(v * 10 ** dec) / 10 ** dec
    s = ("{:,.%df}" % dec).format(scaled) if group else ("{:.%df}" % dec).format(scaled)
    return s + ("%" if pct else "")


# ── Dual-mode scalars ─────────────────────────────────────────────────────────
# Each value carries BOTH its DAX expression and its sample result. Geometry is
# written once, in ordinary Python arithmetic, and produces both outputs.

def _num_lit(x) -> str:
    if isinstance(x, int) or (isinstance(x, float) and float(x).is_integer()):
        return str(int(x))
    return repr(round(float(x), 4))


class Num:
    """A number that is a DAX expression and a sample value at the same time."""
    __slots__ = ("dax", "val")

    def __init__(self, dax: str, val):
        self.dax, self.val = dax, float(val)

    @staticmethod
    def of(x):
        if isinstance(x, Num):
            return x
        return Num(_num_lit(x), x)

    def _bin(self, other, op, pyop):
        o = Num.of(other)
        return Num("( %s %s %s )" % (self.dax, op, o.dax), pyop(self.val, o.val))

    def __add__(self, o):
        return self._bin(o, "+", lambda a, b: a + b)

    def __radd__(self, o):
        return Num.of(o)._bin(self, "+", lambda a, b: a + b)

    def __sub__(self, o):
        return self._bin(o, "-", lambda a, b: a - b)

    def __rsub__(self, o):
        return Num.of(o)._bin(self, "-", lambda a, b: a - b)

    def __mul__(self, o):
        return self._bin(o, "*", lambda a, b: a * b)

    def __rmul__(self, o):
        return Num.of(o)._bin(self, "*", lambda a, b: a * b)

    def __neg__(self):
        return Num("( - %s )" % self.dax, -self.val)

    def __truediv__(self, o):
        """DIVIDE(), not "/" — a zero denominator must yield 0, not an error
        that would poison the whole string concatenation."""
        o = Num.of(o)
        return Num("DIVIDE ( %s, %s )" % (self.dax, o.dax),
                   0.0 if o.val == 0 else self.val / o.val)

    def __rtruediv__(self, o):
        return Num.of(o).__truediv__(self)

    def min(self, o):
        o = Num.of(o)
        return Num("MIN ( %s, %s )" % (self.dax, o.dax), min(self.val, o.val))

    def max(self, o):
        o = Num.of(o)
        return Num("MAX ( %s, %s )" % (self.dax, o.dax), max(self.val, o.val))

    def clamp(self, lo, hi):
        return self.max(lo).min(hi)

    def fmt(self, pattern: str = "0.0") -> "Txt":
        return Txt('FORMAT ( %s, "%s", "en-US" )' % (self.dax, pattern),
                   py_format(self.val, pattern))

    def __repr__(self):
        return "Num(%r, %r)" % (self.dax, self.val)


class Txt:
    """A string that is a DAX string expression and a sample string at once."""
    __slots__ = ("dax", "val")

    def __init__(self, dax: str, val):
        self.dax, self.val = dax, str(val)

    @staticmethod
    def of(x):
        if isinstance(x, Txt):
            return x
        if isinstance(x, Num):
            return x.fmt("0.0")
        return lit(x)

    def __add__(self, o):
        o = Txt.of(o)
        val = self.val + o.val
        # two literals are one literal: without this every lit() fragment of
        # the document becomes its own operand and the emitted DAX triples in
        # size for no gain
        if _is_literal(self) and _is_literal(o):
            return Txt(dax_str(val), val)
        return Txt("%s & %s" % (self.dax, o.dax), val)

    def __radd__(self, o):
        return Txt.of(o).__add__(self)

    def __repr__(self):
        return "Txt(%r, %r)" % (self.dax, self.val)


def lit(s) -> Txt:
    """A literal piece of SVG."""
    return Txt(dax_str(s), str(s))


def num(x) -> Num:
    return Num.of(x)


def measure(name: str, sample) -> Num:
    """A numeric measure reference: [Flights this period]."""
    return Num("[%s]" % name, sample)


def text_measure(name: str, sample) -> Txt:
    """A text measure reference: [Flights change label]."""
    return Txt("[%s]" % name, sample)


def join(parts) -> Txt:
    out = None
    for p in parts:
        p = Txt.of(p)
        out = p if out is None else out + p
    return out if out is not None else lit("")


# ── XML escaping of data-derived text ─────────────────────────────────────────
# A category label containing an ampersand or an angle bracket is enough to
# blank a card. Model data is never written into the document raw.

def _is_literal(t: "Txt") -> bool:
    return t.dax == dax_str(t.val)


def xml_text(t) -> Txt:
    """Escape a data-derived value for use as SVG text content. The ampersand
    goes first — otherwise the ampersand of &lt; gets escaped a second time.
    A compile-time literal is escaped in Python, so a fixed title does not pay
    for three SUBSTITUTE calls per refresh."""
    t = Txt.of(t)
    if _is_literal(t):
        return lit(t.val.replace("&", "&amp;").replace("<", "&lt;")
                   .replace(">", "&gt;"))
    d = t.dax
    for ch, ent in (("&", "&amp;"), ("<", "&lt;"), (">", "&gt;")):
        d = 'SUBSTITUTE ( %s, "%s", "%s" )' % (d, ch, ent)
    v = t.val.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return Txt(d, v)


def xml_attr(t) -> Txt:
    """Escape a data-derived value for a single-quoted SVG attribute."""
    t = xml_text(t)
    if _is_literal(t):
        return lit(t.val.replace("'", "&apos;"))
    return Txt("""SUBSTITUTE ( %s, "'", "&apos;" )""" % t.dax,
               t.val.replace("'", "&apos;"))


# ── Text fitting ───────────────────────────────────────────────────────────────

def fit_font_size(t, avail_px: float, sizes=(34, 30, 26, 22, 18),
                  em: float = EM_DIGIT) -> Txt:
    """Largest size in `sizes` whose rendered width fits avail_px, decided at
    REFRESH time from the real string length.

    The rule, emitted as a SWITCH ladder: a string of n characters at size s
    occupies n * s * em pixels, so size s is allowed while
    n <= floor(avail_px / (s * em)). The smallest size is the fallback and may
    still overflow — a 12-digit number has to go somewhere.
    """
    t = Txt.of(t)
    sizes = sorted({int(s) for s in sizes}, reverse=True)
    caps = [(s, int(avail_px // (s * em))) for s in sizes]
    cases = ['LEN ( %s ) <= %d, "%d"' % (t.dax, cap, s) for s, cap in caps[:-1]]
    val = str(sizes[-1])
    for s, cap in caps:
        if len(t.val) <= cap:
            val = str(s)
            break
    if not cases:
        return Txt('"%d"' % sizes[-1], val)
    return Txt('SWITCH ( TRUE (), %s, "%d" )' % (", ".join(cases), sizes[-1]), val)


def fit_truncate(t, avail_px: float, size: float, em: float = EM_LABEL) -> Txt:
    """Clip a data-derived label to the characters that fit, with an ellipsis.
    Same metric as fit_font_size: n_max = floor(avail_px / (size * em))."""
    t = Txt.of(t)
    n = max(1, int(avail_px // (size * em)))
    keep = max(1, n - 1)
    d = ('IF ( LEN ( %s ) > %d, LEFT ( %s, %d ) & "…", %s )'
         % (t.dax, n, t.dax, keep, t.dax))
    v = t.val if len(t.val) <= n else t.val[:keep] + "…"
    return xml_text(Txt(d, v))


def text_width(s, size: float, em: float = EM_LABEL) -> float:
    """Python-side width estimate, for laying out marks around fixed text."""
    return len(str(s)) * size * em


# ── Series (repeated marks) ───────────────────────────────────────────────────

class Row:
    """One row of a series inside the mark body. In the DAX pass its columns are
    column references; in a sample pass they are that row's values."""

    def __init__(self, doc, series, index: int, dax_mode: bool):
        self._doc, self._s, self.index, self._dax = doc, series, index, dax_mode
        self.lets = []
        self.i = Num("( [Value] - 1 )", index) if dax_mode else Num.of(index)
        self.n = series.n
        self.first = index == 0
        self.last = index == series.n - 1

    def col(self, name: str):
        """A series column. Numeric columns come back as Num, text as Txt."""
        sample = self._s.samples[self.index][name]
        ref = "[%s]" % name
        if isinstance(sample, str):
            return Txt(ref, sample) if self._dax else lit(sample)
        return Num(ref, sample) if self._dax else Num.of(sample)

    def let(self, name: str, value):
        """Hoist a repeated sub-expression into a row-local VAR, so the emitted
        body stays readable instead of inlining the same maths three times."""
        if not self._dax:
            return value
        vname = "_%s%d" % (name, len(self.lets))
        if isinstance(value, Txt):
            self.lets.append((vname, value.dax))
            return Txt(vname, value.val)
        value = Num.of(value)
        self.lets.append((vname, value.dax))
        return Num(vname, value.val)

    def pick(self, cond_dax: str, cond_py: bool, a, b):
        """IF() over dual-mode values — e.g. highlight the last bar."""
        if isinstance(a, Txt) or isinstance(b, Txt):
            a, b = Txt.of(a), Txt.of(b)
            return Txt("IF ( %s, %s, %s )" % (cond_dax, a.dax, b.dax),
                       a.val if cond_py else b.val)
        a, b = Num.of(a), Num.of(b)
        return Num("IF ( %s, %s, %s )" % (cond_dax, a.dax, b.dax),
                   a.val if cond_py else b.val)

    def is_last(self, a, b):
        """Pick `a` on the newest row, `b` elsewhere — the highlight rule every
        sparkline in here uses."""
        return self.pick("[Value] = %d" % self._s.n, self.last, a, b)

    def by_index(self, values):
        """One value per row position, chosen by row number — slice colours,
        mostly. `values` is recycled if it is shorter than the series."""
        vals = [values[k % len(values)] for k in range(self.n)]
        if not self._dax:
            return vals[self.index]
        txt = any(isinstance(v, Txt) for v in vals)
        cast = Txt.of if txt else Num.of
        cases = ", ".join("%d, %s" % (k + 1, cast(v).dax)
                          for k, v in enumerate(vals[:-1]))
        last = cast(vals[-1]).dax
        dax = ("SWITCH ( [Value], %s, %s )" % (cases, last) if cases
               else last)
        sample = cast(vals[self.index]).val
        return Txt(dax, sample) if txt else Num(dax, sample)

    def cum(self, name: str):
        """The running total of a column over the rows BEFORE this one — what a
        donut needs to know where its slice starts."""
        before = sum(r[name] for r in self._s.samples[:self.index])
        if not self._dax:
            return Num.of(before)
        k = self.let("k", Num("[Value]", self.index + 1))
        return self.let("cum", Num(
            "SUMX ( FILTER ( %s, [Value] < %s ), [%s] )"
            % (self._s.var, k.dax, name), before))


class Series:
    """A repeated mark set: one DAX table expression plus the sample rows that
    mirror it. The row COUNT is fixed when the measure is composed, because the
    geometry (bar pitch, arc step) is computed in Python from it."""

    def __init__(self, doc, table_dax: str, samples: list, var: str):
        self.doc, self.table_dax, self.samples, self.var = doc, table_dax, samples, var
        self.n = len(samples)

    def maxx(self, name: str, floor: float = 0.0) -> Num:
        vals = [r[name] for r in self.samples]
        return self.doc.var("max", "MAX ( MAXX ( %s, [%s] ), %s )"
                            % (self.var, name, _num_lit(floor)),
                            max(max(vals), floor))

    def sumx(self, name: str) -> Num:
        return self.doc.var("sum", "SUMX ( %s, [%s] )" % (self.var, name),
                            sum(r[name] for r in self.samples))

    def concat(self, body, sep: str = "") -> Txt:
        """Emit the marks: CONCATENATEX over the series in DAX, the same body
        evaluated row by row for the sample render. `sep` matters for a
        points='x,y x,y' list, where the marks are coordinates, not elements."""
        drow = Row(self.doc, self, 0, dax_mode=True)
        dbody = Txt.of(body(drow))
        inner = dbody.dax
        if drow.lets:
            inner = ("".join("VAR %s = %s " % (n, e) for n, e in drow.lets)
                     + "RETURN " + inner)
        sample = sep.join(Txt.of(body(Row(self.doc, self, i, False))).val
                          for i in range(self.n))
        return self.doc.var(
            "marks",
            "CONCATENATEX ( %s, %s, %s, [Value], ASC )"
            % (self.var, inner, dax_str(sep)),
            sample, kind="txt")


# ── Document ───────────────────────────────────────────────────────────────────

class SvgCard:
    """The composed measure. Iterating it yields exactly
    (dax_expression, width, height) — the primitive contract — while .svg
    carries the offline sample render for the harness."""

    def __init__(self, dax: str, width: float, height: float, svg: str, name: str):
        self.dax = dax
        self.width = width
        self.height = height
        self.svg = svg
        self.name = name

    def __iter__(self):
        return iter((self.dax, self.width, self.height))

    def __len__(self):
        return 3

    def __getitem__(self, i):
        return (self.dax, self.width, self.height)[i]

    def __repr__(self):
        return ("SvgCard(%r, %gx%g, %d chars of DAX)"
                % (self.name, self.width, self.height, len(self.dax)))


def _fnum(x) -> str:
    """Geometry written into literal SVG — never locale-dependent."""
    x = float(x)
    return str(int(x)) if x.is_integer() else ("%.2f" % x).rstrip("0").rstrip(".")


class SvgDoc:
    """Builds one SVG-measure document.

    width/height are the HOST CARD's size in report pixels and are written into
    the <svg> element verbatim; the viewBox is `pad` larger on each axis so a
    stroke on the outer edge is not clipped, and preserveAspectRatio='none'
    stretches that viewBox back onto the card. Keeping those two in step is
    what _validate_svg_measures.py checks.
    """

    def __init__(self, width: float, height: float, tokens: dict,
                 pad: float = 6.0, name: str = "svg"):
        self.width, self.height, self.pad, self.name = width, height, pad, name
        self.tokens = dict(tokens or {})
        self._vars = []          # (varname, dax)
        self._seen = {}          # dax -> varname (deduplicated)
        self._n = 0
        self.parts = []

    # ── VARs ──────────────────────────────────────────────────────────────────

    def var(self, prefix: str, dax: str, val, kind: str = "num"):
        """Hoist an expression into a VAR, reusing one if the expression is
        already declared (colour measures get referenced many times)."""
        if dax in self._seen:
            name = self._seen[dax]
        else:
            self._n += 1
            name = "_%s%d" % (prefix, self._n)
            self._vars.append((name, dax))
            self._seen[dax] = name
        return Txt(name, val) if kind == "txt" else Num(name, val)

    def color(self, token: str) -> Txt:
        """A theme colour, read through its "Color <token>" measure so one
        slicer click re-themes every mark that uses it."""
        if token not in self.tokens:
            raise KeyError("unknown colour token %r; known: %s"
                           % (token, sorted(self.tokens)))
        return self.var("c", "[Color %s]" % token, self.tokens[token], kind="txt")

    def measure(self, name: str, sample) -> Num:
        return self.var("m", "[%s]" % name, sample)

    def text_measure(self, name: str, sample) -> Txt:
        return self.var("t", "[%s]" % name, sample, kind="txt")

    def value(self, ref, sample=None) -> Num:
        """Accept a measure name, a ("measure name", sample) pair, or a plain
        number — so a primitive can be previewed before its measures exist."""
        if isinstance(ref, (tuple, list)) and len(ref) == 2:
            return self.measure(ref[0], ref[1])
        if isinstance(ref, Num):
            return ref
        if isinstance(ref, str):
            if sample is None:
                raise ValueError('measure %r needs a sample value: pass '
                                 '("%s", <sample>)' % (ref, ref))
            return self.measure(ref, sample)
        return Num.of(ref)

    def text(self, ref, sample=None) -> Txt:
        """Same, for text: a ("measure", "sample") pair or a plain string."""
        if isinstance(ref, (tuple, list)) and len(ref) == 2:
            return self.text_measure(ref[0], ref[1])
        if isinstance(ref, Txt):
            return ref
        if isinstance(ref, str) and sample is not None:
            return self.text_measure(ref, sample)
        return lit(ref)

    def color_value(self, ref, sample=None) -> Txt:
        """A colour that may come from a token, a measure returning a hex
        string, or a literal hex."""
        if isinstance(ref, (tuple, list)) and len(ref) == 2:
            return self.text_measure(ref[0], ref[1])
        if isinstance(ref, Txt):
            return ref
        if isinstance(ref, str) and ref.startswith("#"):
            return lit(ref)
        if isinstance(ref, str) and ref in self.tokens:
            return self.color(ref)
        if isinstance(ref, str) and sample is not None:
            return self.text_measure(ref, sample)
        raise ValueError("cannot read a colour from %r" % (ref,))

    # ── series ────────────────────────────────────────────────────────────────

    def series(self, columns: dict, samples: list) -> Series:
        """Declare a repeated mark set.

        columns : {"@v": "<DAX scalar expression per row>", ...} — evaluated in
                  the row context of GENERATESERIES(1, n)
        samples : [{"@v": 12.0, "@lab": "Jan"}, ...] — one dict per row; its
                  length fixes n.
        """
        n = len(samples)
        if n < 1:
            raise ValueError("a series needs at least one sample row")
        missing = sorted(k for k in columns if any(k not in s for s in samples))
        if missing:
            raise ValueError("every sample row needs the series columns; "
                             "missing %s" % missing)
        cols = "".join(',\n        "%s", %s' % (k, v) for k, v in columns.items())
        table = "ADDCOLUMNS ( GENERATESERIES ( 1, %d )%s\n    )" % (n, cols)
        self._n += 1
        var = "_rows%d" % self._n
        self._vars.append((var, table))
        return Series(self, table, samples, var)

    # ── content ───────────────────────────────────────────────────────────────

    def add(self, *parts) -> "SvgDoc":
        for p in parts:
            if p is None:
                continue
            self.parts.append(Txt.of(p))
        return self

    def rect(self, x, y, w, h, fill, rx=0, opacity=None) -> "SvgDoc":
        """A literal-geometry rectangle with a (possibly live) fill."""
        s = ("<rect x='%s' y='%s' width='%s' height='%s'"
             % (_fnum(x), _fnum(y), _fnum(w), _fnum(h)))
        if rx:
            s += " rx='%s'" % _fnum(rx)
        if opacity is not None:
            s += " opacity='%s'" % _fnum(opacity)
        return self.add(lit(s + " fill='"), Txt.of(fill), lit("'/>"))

    def label(self, x, y, text, size, fill, weight=400, anchor="start",
              opacity=None) -> "SvgDoc":
        """A text element with literal placement and (possibly live) content.
        `text` must already be xml-escaped if it comes from the model."""
        s = ("<text x='%s' y='%s' font-family='%s' font-size='%s' "
             "font-weight='%d' text-anchor='%s'"
             % (_fnum(x), _fnum(y), FONT, _fnum(size), weight, anchor))
        if opacity is not None:
            s += " opacity='%s'" % _fnum(opacity)
        return self.add(lit(s + " fill='"), Txt.of(fill), lit("'>"),
                        Txt.of(text), lit("</text>"))

    def header(self) -> str:
        vb_w = round(self.width + self.pad, 2)
        vb_h = round(self.height + self.pad, 2)
        return ("<svg xmlns='http://www.w3.org/2000/svg' width='%s' height='%s' "
                "viewBox='0 0 %s %s' preserveAspectRatio='none'>"
                % (_fnum(self.width), _fnum(self.height), _fnum(vb_w), _fnum(vb_h)))

    def body(self) -> Txt:
        return join([lit(self.header())] + self.parts + [lit("</svg>")])

    def build(self) -> SvgCard:
        body = self.body()
        lines = ["VAR %s =\n    %s" % (n, d) if "\n" in d else "VAR %s = %s" % (n, d)
                 for n, d in self._vars]
        lines.append("VAR _svg =\n    " + body.dax.replace(" & ", "\n        & "))
        lines.append('RETURN\n    "data:image/svg+xml;utf8," & '
                     'SUBSTITUTE ( SUBSTITUTE ( _svg, "%", "%25" ), "#", "%23" )')
        return SvgCard("\n".join(lines), self.width, self.height, body.val, self.name)


# ── Offline render harness ────────────────────────────────────────────────────

def check_svg(svg: str, expect_w=None, expect_h=None) -> dict:
    """Parse an SVG string and return its declared size. Raises on malformed
    XML — which in Power BI would instead be a blank card and no message."""
    root = ET.fromstring(svg)
    tag = root.tag.split("}")[-1]
    if tag != "svg":
        raise ValueError("root element is <%s>, expected <svg>" % tag)
    got = {"width": root.get("width"), "height": root.get("height"),
           "viewBox": root.get("viewBox"),
           "preserveAspectRatio": root.get("preserveAspectRatio")}
    for key, want in (("width", expect_w), ("height", expect_h)):
        if want is None:
            continue
        if got[key] is None:
            raise ValueError("<svg> declares no %s" % key)
        if abs(float(got[key]) - float(want)) > 0.05:
            raise ValueError("svg %s=%s but the card is %s" % (key, got[key], want))
    return got


def render_sample(card: SvgCard, path: str = None) -> str:
    """Render a primitive's sample SVG, assert it is well-formed XML sized to
    its card, and optionally write it out for eyeball review."""
    if not isinstance(card, SvgCard):
        raise TypeError("render_sample expects the SvgCard a primitive returns")
    check_svg(card.svg, card.width, card.height)
    if path:
        d = os.path.dirname(os.path.abspath(path))
        os.makedirs(d, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(card.svg)
    return card.svg


def unescape_uri(s: str) -> str:
    """Reverse the forward escaping. The measure escapes % and then #, so the
    reverse must undo # and then %, or the % recovered from %25 is re-read as
    the start of an escape."""
    return s.replace("%23", "#").replace("%25", "%")


def skeleton(expr: str, placeholder: str = "0") -> str:
    """The literal SVG inside a DAX string concatenation, with every live
    expression collapsed to `placeholder`.

    Only literals at parenthesis depth 0 are document text. That distinction is
    the whole trick: `SUBSTITUTE ( x, "&", "&amp;" )` and
    `FORMAT ( x, "0.0", "en-US" )` are full of string literals that belong to
    the expression, not to the SVG, and splicing them in produces garbage that
    fails to parse for the wrong reason.

    This is also the only thing a gate CAN read offline — the live values are
    not knowable without a model — so the placeholder stands in for each one.
    """
    out, pending = [], False
    i, n, depth = 0, len(expr), 0
    while i < n:
        ch = expr[i]
        if ch == '"':
            j, buf = i + 1, []
            while j < n:
                if expr[j] == '"':
                    if j + 1 < n and expr[j + 1] == '"':
                        buf.append('"')
                        j += 2
                        continue
                    break
                buf.append(expr[j])
                j += 1
            if depth == 0:
                if pending:
                    out.append(placeholder)
                    pending = False
                out.append("".join(buf))
            else:
                pending = True
            i = j + 1
            continue
        if ch == "(":
            depth += 1
            pending = True
        elif ch == ")":
            depth = max(0, depth - 1)
        elif depth == 0:
            if ch == "&":
                if pending:
                    out.append(placeholder)
                    pending = False
            elif not ch.isspace():
                pending = True
        i += 1
    if pending:
        out.append(placeholder)
    return unescape_uri("".join(out))


_VAR_DECL = re.compile(r"VAR\s+([A-Za-z_][A-Za-z0-9_]*)\s*=")
_RETURN = re.compile(r"RETURN\b")


def split_vars(dax: str) -> list:
    """[(name, expression)] for each VAR in a measure body, plus
    ("RETURN", expression) for the final expression.

    Only VAR and RETURN at parenthesis depth zero count. DAX allows a VAR block
    inside a function argument — a CONCATENATEX mark body is usually written
    that way — and treating a nested one as a top-level declaration runs every
    following chunk off its real end.
    """
    marks, depth, i, n = [], 0, 0, len(dax)
    while i < n:
        ch = dax[i]
        if ch == '"':
            i += 1
            while i < n:
                if dax[i] == '"':
                    if i + 1 < n and dax[i + 1] == '"':
                        i += 2
                        continue
                    break
                i += 1
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif depth == 0 and (ch in "VR") and (i == 0 or not (dax[i - 1].isalnum()
                                                            or dax[i - 1] == "_")):
            m = _VAR_DECL.match(dax, i)
            if m:
                marks.append(("var", m.group(1), i, m.end()))
                i = m.end()
                continue
            m = _RETURN.match(dax, i)
            if m:
                marks.append(("return", "RETURN", i, m.end()))
                i = m.end()
                continue
        i += 1

    chunks = []
    for k, (kind, name, start, body_at) in enumerate(marks):
        end = marks[k + 1][2] if k + 1 < len(marks) else len(dax)
        chunks.append((name, dax[body_at:end]))
        if kind == "return":
            break
    if not chunks:
        chunks.append(("RETURN", dax))
    return chunks


def _arg_list(expr: str, open_idx: int):
    """Split a call's argument list, given the index of its '('. Returns
    (args, index just past the matching ')')."""
    args, depth, start, i, n = [], 0, open_idx + 1, open_idx, len(expr)
    while i < n:
        ch = expr[i]
        if ch == '"':
            i += 1
            while i < n:
                if expr[i] == '"':
                    if i + 1 < n and expr[i + 1] == '"':
                        i += 2
                        continue
                    break
                i += 1
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                args.append(expr[start:i])
                return args, i + 1
        elif ch == "," and depth == 1:
            args.append(expr[start:i])
            start = i + 1
        i += 1
    return args, n


_CONCAT = re.compile(r"\bCONCATENATEX\s*\(", re.IGNORECASE)


def concat_bodies(expr: str) -> list:
    """The mark body of every CONCATENATEX in an expression — its second
    argument. Repeated marks are the bulk of an SVG measure's markup and they
    live two levels down inside a function call, so a skeleton taken at depth
    zero never sees them."""
    out = []
    for m in _CONCAT.finditer(expr):
        args, _end = _arg_list(expr, m.end() - 1)
        if len(args) >= 2:
            out.append(args[1])
    return out


def markup_fragments(dax: str, placeholder: str = "0") -> list:
    """[(where, skeleton)] for every piece of markup in a measure that is NOT
    the top-level document: mark bodies, and any VAR that assembles elements of
    its own. Each is a fragment, so it only parses once wrapped."""
    frags = []
    for name, expr in split_vars(dax):
        for k, body in enumerate(concat_bodies(expr)):
            sk = skeleton(body, placeholder)
            if "<" in sk:
                frags.append(("%s mark body%s" % (name, "" if k == 0 else " %d" % (k + 1)), sk))
        sk = skeleton(expr, placeholder)
        if "<" in sk and not ("<svg" in sk and "</svg>" in sk):
            frags.append((name, sk))
    return frags


def _top_level_calls(expr: str) -> list:
    """The argument lists of every call at depth zero of `expr`."""
    out, depth, i, n = [], 0, 0, len(expr)
    while i < n:
        ch = expr[i]
        if ch == '"':
            i += 1
            while i < n:
                if expr[i] == '"':
                    if i + 1 < n and expr[i + 1] == '"':
                        i += 2
                        continue
                    break
                i += 1
        elif ch == "(":
            if depth == 0:
                args, end = _arg_list(expr, i)
                out.append(args)
                i = end
                continue
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        i += 1
    return out


def _find_docs(expr: str, placeholder: str, depth: int, out: list) -> None:
    sk = skeleton(expr, placeholder)
    if "<svg" in sk and "</svg>" in sk:
        # the data-URI prefix is often in the same literal as the document —
        # anything outside <svg>...</svg> is not part of it
        sk = sk[sk.index("<svg"):sk.rindex("</svg>") + len("</svg>")]
        if sk not in out:
            out.append(sk)
        return
    if depth <= 0:
        return
    for args in _top_level_calls(expr):
        for a in args:
            _find_docs(a, placeholder, depth - 1, out)


def svg_documents(dax: str, placeholder: str = "0") -> list:
    """Every complete <svg> document a measure can return, as parseable
    skeletons.

    Normally there is exactly one, assembled in a VAR. But a measure may pick
    between documents — `IF ( theme = "Dark", "<svg A>", "<svg B>" )` — and
    each branch is a card that can break on its own, so the search descends
    through call arguments and returns all of them.
    """
    out = []
    for _name, expr in split_vars(dax):
        _find_docs(expr, placeholder, 6, out)
    return out


def svg_skeleton_from_dax(dax: str, placeholder: str = "0") -> str:
    """The SVG document inside a measure, as a parseable skeleton. Raises if
    there is none."""
    docs = svg_documents(dax, placeholder)
    if docs:
        return docs[0]
    raise ValueError(
        "no complete <svg> document found in the measure: assemble it in one "
        "VAR (VAR _svg = \"<svg ...>\" & ... & \"</svg>\") so it can be "
        "validated without a model")


def live_svg(dax: str) -> str:
    """Recover the SVG skeleton from an emitted measure, the way the gate does."""
    return svg_skeleton_from_dax(dax)


# ══════════════════════════════════════════════════════════════════════════════
# Primitives
# ══════════════════════════════════════════════════════════════════════════════
# Each one lays its geometry out in Python and returns an SvgCard: a DAX
# expression, the card size it must be hosted at, and the sample render.
#
# Every primitive takes `tokens` — the colour-token mapping (see
# brand_styles.svg_tokens) — and reads its colours through `Color <token>`
# measures, so a Light/Dark slicer click restyles it with no rebuild.
#
# Every primitive documents its TEXT-FITTING RULE. SVG has no layout engine, so
# this is never left to chance.

PAD = 18.0            # card inner padding
TITLE_SIZE = 13.0     # card title / row label
LABEL_SIZE = 9.5      # axis tick label

# Simple line glyphs drawn for a 24x24 box, stroked (never filled) so they take
# the chip's ink colour. Deliberately plain: an icon set with a licence is a
# dependency this generator does not need.
ICONS = {
    "trend":    "<path d='M3 17l6-6 4 4 8-8'/><path d='M21 7h-5m5 0v5'/>",
    "clock":    "<circle cx='12' cy='12' r='9'/><path d='M12 7v5l4 2'/>",
    "users":    ("<circle cx='9' cy='8' r='3.5'/>"
                 "<path d='M3 20c0-3.3 2.7-6 6-6s6 2.7 6 6'/>"
                 "<path d='M16 5.5a3.5 3.5 0 0 1 0 7'/>"),
    "target":   "<circle cx='12' cy='12' r='9'/><circle cx='12' cy='12' r='4'/>",
    "check":    "<path d='M4 13l5 5L20 6'/>",
    "flag":     "<path d='M6 21V4h12l-3 5 3 5H6'/>",
    "stack":    ("<path d='M4 8l8-4 8 4-8 4-8-4z'/><path d='M4 12l8 4 8-4'/>"
                 "<path d='M4 16l8 4 8-4'/>"),
    "bolt":     "<path d='M13 2L5 14h6l-1 8 8-12h-6l1-8z'/>",
}


class SeriesSpec(tuple):
    """The data behind a repeated mark set: the per-row DAX plus the sample
    rows that mirror it.

        SeriesSpec({"@v": "CALCULATE ( [Hires], ... )",
                    "@lab": "FORMAT ( [Value], \"0\" )"},
                   [{"@v": 12, "@lab": "Jan"}, ...])

    The sample rows fix the mark COUNT, because the geometry (bar pitch, slice
    step, point spacing) is computed in Python from it.
    """
    __slots__ = ()

    def __new__(cls, columns, samples):
        if not isinstance(columns, dict):
            raise TypeError("SeriesSpec(columns, samples): columns is a dict of "
                            "column name -> per-row DAX")
        return super().__new__(cls, (dict(columns), [dict(s) for s in samples]))

    @property
    def columns(self):
        return self[0]

    @property
    def samples(self):
        return self[1]

    def __len__(self):
        return len(self[1])


def _spec(series) -> SeriesSpec:
    if isinstance(series, SeriesSpec):
        return series
    if isinstance(series, (tuple, list)) and len(series) == 2:
        return SeriesSpec(series[0], series[1])
    raise TypeError("series must be a SeriesSpec(columns, samples) "
                    "or a (columns, samples) pair")


def _tokens(tokens):
    if tokens is None:
        return dict(TOKENS_LIGHT)
    missing = sorted(set(SVG_TOKENS) - set(tokens))
    if missing:
        raise ValueError("colour tokens missing: %s — build the mapping with "
                         "brand_styles.svg_tokens()" % missing)
    return dict(tokens)


def _doc(w, h, tokens, name, pad=6.0) -> SvgDoc:
    return SvgDoc(float(w), float(h), _tokens(tokens), pad=pad, name=name)


def _bg(doc: SvgDoc, bg, radius) -> None:
    """The card surface. bg=None leaves it to the page background."""
    if bg:
        doc.rect(0, 0, doc.width, doc.height, doc.color_value(bg), rx=radius)


def _icon(doc: SvgDoc, name: str, cx, cy, size=22.0, color="chipicon") -> None:
    """One glyph, centred on (cx, cy)."""
    inner = ICONS.get(name)
    if inner is None:
        raise KeyError("unknown icon %r; known: %s" % (name, sorted(ICONS)))
    s = size / 24.0
    doc.add(lit("<g transform='translate(%s %s) scale(%s)' fill='none' stroke='"
                % (_fnum(cx - size / 2), _fnum(cy - size / 2), _fnum(round(s, 4)))),
            doc.color(color),
            lit("' stroke-width='2' stroke-linecap='round' "
                "stroke-linejoin='round'>" + inner + "</g>"))


def _chip(doc: SvgDoc, icon, cx, cy, r=24.0) -> None:
    if not icon:
        return
    doc.add(lit("<circle cx='%s' cy='%s' r='%s' fill='"
                % (_fnum(cx), _fnum(cy), _fnum(r))),
            doc.color("chip"), lit("'/>"))
    _icon(doc, icon, cx, cy, size=r * 0.92)


def _anim(attr, frm, to, begin=0.0, dur=0.6, extra="") -> Txt:
    """One SMIL animation, held at its end state. `fill='freeze'` is what stops
    the mark snapping back to its from-value when the timeline ends.

    `begin` may be a live value: inside a series body the stagger has to be
    computed from [Value], because the Python row index is 0 for every row of
    the DAX pass — a constant begin makes every mark animate in unison.
    """
    b = begin if isinstance(begin, (Num, Txt)) else Num.of(begin)
    btxt = b.fmt("0.00") if isinstance(b, Num) else b
    return (lit("<animate attributeName='%s' from='" % attr) + Txt.of(frm)
            + lit("' to='") + Txt.of(to) + lit("' begin='") + btxt
            + lit("s' dur='%ss' fill='freeze'%s/>" % (_fnum(dur), extra)))


def kpi_sparkbar(w, h, title, value, series=None, change=None,
                 change_color="positive", value_format="#,0", icon=None,
                 tokens=None, bg="card", radius=16, spark_share=0.32,
                 animate=True, name="kpi") -> SvgCard:
    """KPI tile: title, one big number, an optional change chip, and a column
    sparkbar of the trend behind it.

        kpi_sparkbar(348, 119, "Flights",
                     value=("Flights this period", 1284),
                     change=("Flights change label", "+8.2%"),
                     change_color="Flights change color",
                     icon="trend",
                     series=SeriesSpec({"@v": "...", "@lab": "..."}, [...]))

    The newest column is drawn in `accent`, the rest in `accentsoft`; each
    column grows from zero, staggered 0.07s apart.

    TEXT FITTING. The number gets the width between its own x and the sparkbar,
    less the measured width of the change label, and takes the largest size
    from 34/28/24/20/16 that fits at the live string length (fit_font_size).
    The change label is a tspan with dx, so it FLOWS after the number instead
    of being placed at a fixed x that a wider number would overrun.
    """
    doc = _doc(w, h, tokens, name)
    _bg(doc, bg, radius)
    v = doc.value(value)
    vtxt = xml_text(v.fmt(value_format))

    doc.add(lit("<text x='%s' y='%s' font-family='%s' font-size='%s' "
                "font-weight='600' fill='" % (_fnum(PAD), _fnum(PAD + 8), FONT,
                                              _fnum(TITLE_SIZE))),
            doc.color("muted"), lit("'>"), xml_text(doc.text(title)),
            lit("</text>"))

    chip_r = 23.0
    val_x = PAD
    base_y = h - PAD - 20
    if icon:
        _chip(doc, icon, PAD + chip_r, base_y - 11, chip_r)
        val_x = PAD + chip_r * 2 + 14

    spark_w = max(0.0, (w - 2 * PAD) * spark_share) if series else 0.0
    spark_x = w - PAD - spark_w
    change_w = text_width(Txt.of(doc.text(change)).val if change else "",
                          14, EM_DIGIT) + 12 if change else 0.0
    avail = max(40.0, (spark_x - 10 if series else w - PAD) - val_x - change_w)
    size = fit_font_size(vtxt, avail, sizes=(34, 28, 24, 20, 16))

    doc.add(lit("<text x='%s' y='%s' font-family='%s'><tspan font-size='"
                % (_fnum(val_x), _fnum(base_y), FONT)),
            size, lit("' font-weight='600' fill='"), doc.color("ink"),
            lit("'>"), vtxt, lit("</tspan>"))
    if change:
        doc.add(lit("<tspan dx='10' font-size='14' font-weight='600' fill='"),
                doc.color_value(change_color, "#15803D"), lit("'>"),
                xml_text(doc.text(change)), lit("</tspan>"))
    doc.add(lit("</text>"))

    if series:
        spec = _spec(series)
        rows = doc.series(spec.columns, spec.samples)
        n = rows.n
        top = PAD + 24.0
        floor_y = h - PAD - 14.0
        plot_h = max(12.0, floor_y - top)
        bw = min(14.0, spark_w / n * 0.66)
        pitch = (spark_w - bw) / max(n - 1, 1)
        mx = rows.maxx("@v", 1)
        accent, soft, muted = doc.color("accent"), doc.color("accentsoft"), doc.color("muted")
        has_lab = "@lab" in spec.columns
        # tick labels are thinned, never shrunk: draw every k-th, counting back
        # from the newest column so the period that matters is always labelled
        need = max(text_width(s.get("@lab", ""), LABEL_SIZE) for s in spec.samples) + 4
        step = max(1, int(math.ceil(need / max(pitch, 1.0))))

        def bar(r):
            bh = r.let("h", (r.col("@v") / mx).clamp(0, 1) * plot_h)
            x = spark_x + r.i * pitch
            y = floor_y - bh
            out = (lit("<rect x='") + x.fmt("0.0")
                   + lit("' y='") + (y if animate else y).fmt("0.0")
                   + lit("' width='%s' height='" % _fnum(bw))
                   + (lit("0") if animate else bh.fmt("0.0"))
                   + lit("' rx='2.5' fill='") + r.is_last(accent, soft)
                   + lit("'>"))
            if animate:
                stagger = r.i * 0.07
                out = (out + _anim("height", 0, bh.fmt("0.0"), stagger)
                       + _anim("y", floor_y, y.fmt("0.0"), stagger))
            out = out + lit("</rect>")
            if has_lab:
                shown = r.pick("MOD ( %d - [Value], %d ) = 0" % (n, step),
                               (n - 1 - r.index) % step == 0,
                               xml_text(r.col("@lab")), lit(""))
                out = (out + lit("<text x='") + (x + bw / 2).fmt("0.0")
                       + lit("' y='%s' text-anchor='middle' font-family='%s' "
                             "font-size='%s' fill='"
                             % (_fnum(h - PAD), FONT, _fnum(LABEL_SIZE)))
                       + muted + lit("'>") + shown + lit("</text>"))
            return out

        doc.add(rows.concat(bar))
    return doc.build()


def _arc_path(cx, cy, r, sweep):
    """An arc centred on the upward vertical, spanning `sweep` degrees.
    Returns (d, length). Angles run clockwise from 12 o'clock, so the gap sits
    at the bottom where a gauge's gap belongs."""
    half = math.radians(sweep / 2.0)
    x0, y0 = cx - r * math.sin(half), cy - r * math.cos(half)
    x1, y1 = cx + r * math.sin(half), cy - r * math.cos(half)
    large = 1 if sweep > 180 else 0
    d = ("M %s %s A %s %s 0 %d 1 %s %s"
         % (_fnum(round(x0, 2)), _fnum(round(y0, 2)), _fnum(r), _fnum(r),
            large, _fnum(round(x1, 2)), _fnum(round(y1, 2))))
    return d, r * math.radians(sweep)


def gauge_arc(w, h, value, max_value=1.0, title=None, caption=None,
              value_format="0%", tokens=None, bg="card", radius=16,
              sweep=240.0, thickness=None, min_label=None, max_label=None,
              animate=True, name="gauge") -> SvgCard:
    """Radial gauge: a track arc with the value arc drawn over it, and the
    value in the middle.

        gauge_arc(348, 240, value=("Offer accept rate", 0.62),
                  title="Offer acceptance", caption="of 148 offers",
                  min_label="0%", max_label="100%")

    The value arc draws itself with stroke-dasharray / stroke-dashoffset: the
    dash is the whole arc length, so the offset is how much of it is still
    hidden. The static offset is already the final one, so a renderer that
    ignores SMIL still shows the right value.

    TEXT FITTING. The centre value gets 1.5x the arc radius (the width of the
    hole) and takes the largest of 40/34/28/22/18 that fits; the caption under
    it is truncated to the same width at 12px. End labels are literal and are
    the caller's to keep short.
    """
    doc = _doc(w, h, tokens, name)
    _bg(doc, bg, radius)
    top = PAD + (26.0 if title else 0.0)
    if title:
        doc.label(PAD, PAD + 8, xml_text(doc.text(title)), TITLE_SIZE,
                  doc.color("muted"), weight=600)

    ends = 14.0 if (min_label or max_label) else 0.0
    half = math.radians(sweep / 2.0)
    span_w, span_h = 2 * math.sin(half), 1 + max(0.0, -math.cos(half))
    r = min((w - 2 * PAD) / span_w, (h - top - PAD - ends) / span_h) * 0.94
    t = float(thickness) if thickness else max(9.0, r * 0.17)
    r -= t / 2
    cx, cy = w / 2.0, top + r + t / 2
    d, arc_len = _arc_path(cx, cy, r, sweep)

    frac = (doc.value(value) / doc.value(max_value)).clamp(0, 1)
    hidden = ((1 - frac) * arc_len).fmt("0.0")
    stroke = ("fill='none' stroke-width='%s' stroke-linecap='round'" % _fnum(t))

    doc.add(lit("<path d='%s' %s stroke='" % (d, stroke)), doc.color("track2"),
            lit("'/>"))
    doc.add(lit("<path d='%s' %s stroke-dasharray='%s' stroke-dashoffset='"
                % (d, stroke, _fnum(round(arc_len, 1)))), hidden,
            lit("' stroke='"), doc.color("accent"), lit("'>"))
    if animate:
        doc.add(_anim("stroke-dashoffset", _fnum(round(arc_len, 1)), hidden,
                      0.1, 0.9))
    doc.add(lit("</path>"))

    vtxt = xml_text(doc.value(value).fmt(value_format))
    vsize = fit_font_size(vtxt, r * 1.5, sizes=(40, 34, 28, 22, 18))
    doc.add(lit("<text x='%s' y='%s' font-family='%s' text-anchor='middle' "
                "font-weight='600' font-size='" % (_fnum(cx), _fnum(cy + 10), FONT)),
            vsize, lit("' fill='"), doc.color("ink"), lit("'>"), vtxt,
            lit("</text>"))
    if caption:
        doc.label(cx, cy + 32, fit_truncate(doc.text(caption), r * 1.5, 12),
                  12, doc.color("muted"), anchor="middle")
    base_y = cy - r * math.cos(half) + t / 2 + 13
    if min_label:
        doc.label(cx - r * math.sin(half), base_y, xml_text(doc.text(min_label)),
                  10.5, doc.color("muted"), anchor="middle")
    if max_label:
        doc.label(cx + r * math.sin(half), base_y, xml_text(doc.text(max_label)),
                  10.5, doc.color("muted"), anchor="middle")
    return doc.build()


def bar_list(w, h, series, title=None, value_format="#,0", tokens=None,
             bg="card", radius=16, label_share=0.34, highlight="first",
             animate=True, name="bars") -> SvgCard:
    """Ranked horizontal bars: label, track, bar, value — one row per series
    row. The honest replacement for a clusteredBarChart.

        bar_list(348, 265,
                 SeriesSpec({"@v": "...", "@lab": "..."}, [...]),
                 title="Hires by client", highlight="first")

    highlight: "first" (rank 1 in accent, the rest soft), "last", or "none"
    (every bar in accent). Each bar grows from zero, staggered 0.06s apart.

    TEXT FITTING, two rules.
      * Labels are truncated with an ellipsis to the label column
        (label_share of the inner width) at 12px.
      * The value column is sized from the LONGEST SAMPLE value plus 15%
        headroom, and the value itself is drawn through the font-size ladder
        13/11/10 against that column — so a live value longer than every
        sample steps down instead of colliding with its bar.
    """
    spec = _spec(series)
    doc = _doc(w, h, tokens, name)
    _bg(doc, bg, radius)
    top = PAD + (28.0 if title else 0.0)
    if title:
        doc.label(PAD, PAD + 8, xml_text(doc.text(title)), TITLE_SIZE,
                  doc.color("muted"), weight=600)

    rows = doc.series(spec.columns, spec.samples)
    n = rows.n
    row_h = (h - top - PAD) / n
    bar_h = min(15.0, row_h * 0.48)
    inner = w - 2 * PAD
    label_w = inner * label_share
    val_w = max(26.0, max(text_width(py_format(s["@v"], value_format), 13,
                                     EM_DIGIT) for s in spec.samples) * 1.15)
    bar_x = PAD + label_w + 10
    track_w = max(20.0, w - PAD - val_w - 10 - bar_x)
    mx = rows.maxx("@v", 1)
    accent, soft = doc.color("accent"), doc.color("accentsoft")
    track = doc.color("track2")
    ink, muted = doc.color("ink"), doc.color("muted")
    vsize = fit_font_size(lit(py_format(spec.samples[0]["@v"], value_format)),
                          val_w, sizes=(13, 11, 10), em=EM_DIGIT)

    def row(r):
        cy = top + r.index * row_h + row_h / 2
        y = cy - bar_h / 2
        bw = r.let("w", (r.col("@v") / mx).clamp(0, 1) * track_w)
        if highlight == "none":
            fill = accent
        elif highlight == "last":
            fill = r.is_last(accent, soft)
        else:
            fill = r.pick("[Value] = 1", r.first, accent, soft)
        out = (lit("<text x='%s' y='%s' font-family='%s' font-size='12' "
                   "fill='" % (_fnum(PAD), _fnum(cy + 4), FONT))
               + muted + lit("'>") + fit_truncate(r.col("@lab"), label_w, 12)
               + lit("</text>")
               + lit("<rect x='%s' y='%s' width='%s' height='%s' rx='%s' fill='"
                     % (_fnum(bar_x), _fnum(y), _fnum(track_w), _fnum(bar_h),
                        _fnum(bar_h / 2)))
               + track + lit("'/>")
               + lit("<rect x='%s' y='%s' width='" % (_fnum(bar_x), _fnum(y)))
               + (lit("0") if animate else bw.fmt("0.0"))
               + lit("' height='%s' rx='%s' fill='"
                     % (_fnum(bar_h), _fnum(bar_h / 2)))
               + fill + lit("'>"))
        if animate:
            out = out + _anim("width", 0, bw.fmt("0.0"), r.i * 0.06, 0.7)
        return (out + lit("</rect>")
                + lit("<text x='%s' y='%s' font-family='%s' text-anchor='end' "
                      "font-weight='600' font-size='" % (_fnum(w - PAD),
                                                         _fnum(cy + 4), FONT))
                + vsize + lit("' fill='") + ink + lit("'>")
                + xml_text(r.col("@v").fmt(value_format)) + lit("</text>"))

    doc.add(rows.concat(row))
    return doc.build()


def donut(w, h, series, title=None, center_value=None, center_caption=None,
          value_format="#,0", tokens=None, bg="card", radius=16, hole=0.62,
          palette=("accent", "series2", "accentsoft", "neutral", "track2"),
          legend=True, animate=True, name="donut") -> SvgCard:
    """Donut with an optional legend to its right.

    Each slice is one stroked circle: the dash is the slice, the gap is the
    rest of the circumference, and the dash offset rotates it to where its
    share starts — which is the running total of the slices before it
    (Row.cum). The ring draws itself slice by slice, 0.08s apart.

        donut(348, 265, SeriesSpec({"@v": "...", "@lab": "..."}, [...]),
              title="Pipeline by stage",
              center_value=("Candidates", 446), center_caption="total")

    TEXT FITTING. The centre value gets 0.9x the hole diameter through the
    40/32/26/20 ladder; the caption under it and the legend labels are
    truncated with an ellipsis to their own columns.
    """
    spec = _spec(series)
    doc = _doc(w, h, tokens, name)
    _bg(doc, bg, radius)
    top = PAD + (28.0 if title else 0.0)
    if title:
        doc.label(PAD, PAD + 8, xml_text(doc.text(title)), TITLE_SIZE,
                  doc.color("muted"), weight=600)

    ring_share = 0.46 if legend else 1.0
    side = min(h - top - PAD, (w - 2 * PAD) * ring_share)
    r_out = side / 2 * 0.98
    r_in = r_out * hole
    rm = (r_out + r_in) / 2
    t = r_out - r_in
    circ = 2 * math.pi * rm
    cx, cy = PAD + side / 2, top + side / 2

    rows = doc.series(spec.columns, spec.samples)
    total = rows.sumx("@v")
    colors = [doc.color(c) for c in palette]

    def slice_(r):
        share = r.let("f", (r.col("@v") / total).clamp(0, 1))
        start = r.let("s", (r.cum("@v") / total).clamp(0, 1))
        seg = r.let("g", share * circ)
        dash = seg.fmt("0.0") + lit(" ") + (circ - seg).fmt("0.0")
        out = (lit("<circle cx='%s' cy='%s' r='%s' fill='none' stroke-width='%s'"
                   " transform='rotate(-90 %s %s)' stroke-dashoffset='"
                   % (_fnum(cx), _fnum(cy), _fnum(round(rm, 2)), _fnum(round(t, 2)),
                      _fnum(cx), _fnum(cy)))
               + (-(start * circ)).fmt("0.0")
               + lit("' stroke-dasharray='")
               + (lit("0 %s" % _fnum(round(circ, 1))) if animate else dash)
               + lit("' stroke='") + r.by_index(colors) + lit("'>"))
        if animate:
            out = out + _anim("stroke-dasharray", "0 %s" % _fnum(round(circ, 1)),
                              dash, r.i * 0.08, 0.7)
        return out + lit("</circle>")

    doc.add(rows.concat(slice_))

    if center_value is not None:
        vtxt = xml_text(doc.value(center_value).fmt(value_format))
        doc.add(lit("<text x='%s' y='%s' font-family='%s' text-anchor='middle' "
                    "font-weight='600' font-size='"
                    % (_fnum(cx), _fnum(cy + (2 if center_caption else 8)), FONT)),
                fit_font_size(vtxt, r_in * 1.8, sizes=(40, 32, 26, 20)),
                lit("' fill='"), doc.color("ink"), lit("'>"), vtxt, lit("</text>"))
    if center_caption:
        doc.label(cx, cy + 20, fit_truncate(doc.text(center_caption), r_in * 1.8, 11),
                  11, doc.color("muted"), anchor="middle")

    if legend:
        lx = PAD + side + 16
        lw = max(40.0, w - PAD - lx)
        lh = min(26.0, (h - top - PAD) / rows.n)
        ly0 = cy - (rows.n - 1) * lh / 2
        vw = max(24.0, max(text_width(py_format(s["@v"], value_format), 11.5,
                                      EM_DIGIT) for s in spec.samples) * 1.15)
        muted, ink = doc.color("muted"), doc.color("ink")

        def leg(r):
            y = ly0 + r.index * lh
            return (lit("<circle cx='%s' cy='%s' r='4.5' fill='"
                        % (_fnum(lx + 4.5), _fnum(y)))
                    + r.by_index(colors) + lit("'/>")
                    + lit("<text x='%s' y='%s' font-family='%s' font-size='11.5'"
                          " fill='" % (_fnum(lx + 17), _fnum(y + 4), FONT))
                    + muted + lit("'>")
                    + fit_truncate(r.col("@lab"), lw - 17 - vw - 6, 11.5)
                    + lit("</text>")
                    + lit("<text x='%s' y='%s' font-family='%s' text-anchor='end'"
                          " font-weight='600' font-size='11.5' fill='"
                          % (_fnum(w - PAD), _fnum(y + 4), FONT))
                    + ink + lit("'>")
                    + xml_text(r.col("@v").fmt(value_format)) + lit("</text>"))

        doc.add(rows.concat(leg))
    return doc.build()


def line_area(w, h, series, title=None, caption=None, value_format="#,0",
              tokens=None, bg="card", radius=16, gridlines=3, dots=True,
              area=True, headroom=1.10, animate=True, name="line") -> SvgCard:
    """Line with an area fill under it, gridlines and x labels.

        line_area(444, 295, SeriesSpec({"@v": "...", "@lab": "..."}, [...]),
                  title="CVs per week", caption="last 12 weeks")

    The point list is one CONCATENATEX joined with spaces, consumed by both a
    <polyline> and a <polygon> — the polygon just adds the two baseline
    corners, which are literal. The line draws itself with
    stroke-dasharray/offset over an UPPER BOUND on its length (the exact length
    is not knowable without the live values), so the draw finishes a little
    early rather than stopping short.

    TEXT FITTING. x labels are thinned, not shrunk: every k-th label is drawn,
    counting back from the newest point so the period that matters is always
    labelled, with k chosen to keep the drawn labels at least 38px apart. The
    y-axis is unlabelled by design — the point values live in the tooltip, and
    a crowded axis is worse than none.
    """
    spec = _spec(series)
    doc = _doc(w, h, tokens, name)
    _bg(doc, bg, radius)
    top = PAD + (28.0 if title else 0.0) + (16.0 if caption else 0.0)
    if title:
        doc.label(PAD, PAD + 8, xml_text(doc.text(title)), TITLE_SIZE,
                  doc.color("muted"), weight=600)
    if caption:
        doc.label(PAD, PAD + 26, fit_truncate(doc.text(caption), w - 2 * PAD, 11),
                  11, doc.color("muted"))

    x0, x1 = PAD + 4.0, w - PAD - 4.0
    base = h - PAD - 14.0
    plot_h = max(20.0, base - top)
    rows = doc.series(spec.columns, spec.samples)
    n = rows.n
    pitch = (x1 - x0) / max(n - 1, 1)
    mx = rows.maxx("@v", 1) * headroom

    for g in range(1, gridlines + 1):
        gy = base - plot_h * g / (gridlines + 1)
        doc.add(lit("<rect x='%s' y='%s' width='%s' height='1' fill='"
                    % (_fnum(x0), _fnum(round(gy, 2)), _fnum(x1 - x0))),
                doc.color("line"), lit("'/>"))
    doc.add(lit("<rect x='%s' y='%s' width='%s' height='1' fill='"
                % (_fnum(x0), _fnum(base), _fnum(x1 - x0))),
            doc.color("hairline"), lit("'/>"))

    def point(r):
        x = x0 + r.i * pitch
        y = base - (r.col("@v") / mx).clamp(0, 1) * plot_h
        return x.fmt("0.0") + lit(",") + y.fmt("0.0")

    pts = rows.concat(point, sep=" ")
    accent = doc.color("accent")
    if area:
        doc.add(lit("<polygon points='%s,%s " % (_fnum(x0), _fnum(base))), pts,
                lit(" %s,%s' fill='" % (_fnum(x1), _fnum(base))), accent,
                lit("' opacity='%s'>" % ("0" if animate else "0.14")))
        if animate:
            doc.add(_anim("opacity", 0, "0.14", 0.45, 0.5))
        doc.add(lit("</polygon>"))

    bound = (n - 1) * pitch + plot_h * 2       # upper bound on the path length
    doc.add(lit("<polyline points='"), pts,
            lit("' fill='none' stroke-width='2.5' stroke-linecap='round' "
                "stroke-linejoin='round' stroke='"), accent, lit("'"))
    if animate:
        doc.add(lit(" stroke-dasharray='%s' stroke-dashoffset='0'>" % _fnum(round(bound))),
                _anim("stroke-dashoffset", _fnum(round(bound)), 0, 0.0, 0.9),
                lit("</polyline>"))
    else:
        doc.add(lit("/>"))

    step = max(1, int(math.ceil(38.0 / max(pitch, 1.0))))
    muted = doc.color("muted")
    if dots or "@lab" in spec.columns:
        halo = doc.color("halo")

        def deco(r):
            x = x0 + r.i * pitch
            y = r.let("y", base - (r.col("@v") / mx).clamp(0, 1) * plot_h)
            out = lit("")
            if dots:
                out = (out + lit("<circle cx='") + x.fmt("0.0") + lit("' cy='")
                       + y.fmt("0.0") + lit("' r='3.5' stroke-width='2' stroke='")
                       + halo + lit("' fill='") + accent
                       + lit("' opacity='%s'>" % ("0" if animate else "1")))
                if animate:
                    out = out + _anim("opacity", 0, 1, r.i * 0.05 + 0.5, 0.3)
                out = out + lit("</circle>")
            if "@lab" in spec.columns:
                shown = r.pick("MOD ( %d - [Value], %d ) = 0" % (n, step),
                               (n - 1 - r.index) % step == 0,
                               fit_truncate(r.col("@lab"), pitch * step, 10),
                               lit(""))
                out = (out + lit("<text x='") + x.fmt("0.0")
                       + lit("' y='%s' text-anchor='middle' font-family='%s' "
                             "font-size='%s' fill='"
                             % (_fnum(h - PAD + 2), FONT, _fnum(LABEL_SIZE)))
                       + muted + lit("'>") + shown + lit("</text>"))
            return out

        doc.add(rows.concat(deco))
    return doc.build()


def progress_track(w, h, label, value, max_value=1.0, target=None,
                   value_format="0%", caption=None, tokens=None, bg="card",
                   radius=16, track_h=None, animate=True,
                   name="progress") -> SvgCard:
    """A labelled progress bar with an optional target tick.

        progress_track(348, 110, "Offers accepted",
                       value=("Accept rate", 0.62),
                       target=("Accept target", 0.75),
                       caption="target 75%")

    The fill grows from zero on refresh. The bar is clamped to [0, 1] of the
    track, so a value above max_value saturates instead of drawing past the
    card edge.

    TEXT FITTING. The label takes the left column — the inner width less the
    value's measured sample width — truncated at 13px; the value takes the
    26/22/18/15 ladder against its own column. Both are on one line, so
    neither can push the other off the card.
    """
    doc = _doc(w, h, tokens, name)
    _bg(doc, bg, radius)
    th = float(track_h) if track_h else max(8.0, min(14.0, h * 0.11))
    vtxt = xml_text(doc.value(value).fmt(value_format))
    inner = w - 2 * PAD
    val_w = max(40.0, text_width(vtxt.val, 26, EM_DIGIT) * 1.1)
    vsize = fit_font_size(vtxt, val_w, sizes=(26, 22, 18, 15))

    row_y = PAD + 18.0
    doc.add(lit("<text x='%s' y='%s' font-family='%s' font-size='13' "
                "font-weight='600' fill='" % (_fnum(PAD), _fnum(row_y), FONT)),
            doc.color("muted"), lit("'>"),
            fit_truncate(doc.text(label), inner - val_w - 10, 13),
            lit("</text>"))
    doc.add(lit("<text x='%s' y='%s' font-family='%s' text-anchor='end' "
                "font-weight='600' font-size='" % (_fnum(w - PAD),
                                                   _fnum(row_y + 4), FONT)),
            vsize, lit("' fill='"), doc.color("ink"), lit("'>"), vtxt,
            lit("</text>"))

    cap_h = 16.0 if caption else 0.0
    ty = h - PAD - cap_h - th
    tx, tw = PAD, inner
    doc.rect(tx, ty, tw, th, doc.color("track"), rx=th / 2)
    frac = (doc.value(value) / doc.value(max_value)).clamp(0, 1)
    fw = (frac * tw).fmt("0.0")
    doc.add(lit("<rect x='%s' y='%s' width='" % (_fnum(tx), _fnum(ty)))
            + (lit("0") if animate else fw)
            + lit("' height='%s' rx='%s' fill='" % (_fnum(th), _fnum(th / 2))),
            doc.color("accent"), lit("'>"))
    if animate:
        doc.add(_anim("width", 0, fw, 0.1, 0.8))
    doc.add(lit("</rect>"))

    if target is not None:
        tf = (doc.value(target) / doc.value(max_value)).clamp(0, 1)
        doc.add(lit("<rect x='") + (tx + tf * tw - 1).fmt("0.0")
                + lit("' y='%s' width='2' height='%s' rx='1' fill='"
                      % (_fnum(ty - 3), _fnum(th + 6))),
                doc.color("ink"), lit("' opacity='0.55'/>"))
    if caption:
        doc.label(PAD, h - PAD, fit_truncate(doc.text(caption), inner, 11), 11,
                  doc.color("muted"))
    return doc.build()


PRIMITIVES = {
    "kpi_sparkbar": kpi_sparkbar,
    "gauge_arc": gauge_arc,
    "bar_list": bar_list,
    "donut": donut,
    "line_area": line_area,
    "progress_track": progress_track,
}


# ══════════════════════════════════════════════════════════════════════════════
# Theme measures
# ══════════════════════════════════════════════════════════════════════════════

THEME_TABLE = "Theme"
THEME_FOLDER = "80 Theme"
SVG_FOLDER = "90 SVG"


def theme_table_dax(modes=("Light", "Dark")) -> str:
    """The disconnected table a Light/Dark slicer binds to. Disconnected is the
    point: no relationship, no filtering of anything — the selection is only
    ever read back by a measure."""
    rows = ", ".join('{ "%s", %d }' % (m, i + 1) for i, m in enumerate(modes))
    return ('DATATABLE ( "%s", STRING, "Ordre", INTEGER, { %s } )'
            % (THEME_TABLE, rows))


def add_theme_measures(model, light: dict, dark: dict, table: str = "_Measures",
                       theme_table: str = THEME_TABLE, default: str = "Light",
                       force: bool = False) -> list:
    """Write the Theme table and the colour measures every SVG primitive reads.

        pair = brand_styles.derive_theme_pair("#C89C50")
        add_theme_measures(model, pair["light"], pair["dark"])
        page.add_slicer("Theme", "Theme", ...)        # the toggle

    One `Selected theme` measure reads the slicer; one `Color <token>` measure
    per token picks a side. Nothing else in the report knows which theme is on,
    so one click restyles every card with no rebuild and no bookmark.

    Returns the measure names written.
    """
    missing = sorted((set(SVG_TOKENS) - set(light)) | (set(SVG_TOKENS) - set(dark)))
    if missing:
        raise ValueError("both palettes need every token; missing %s" % missing)
    model.add_calculated_table(
        theme_table, theme_table_dax(),
        [{"name": theme_table, "type": "string", "sort_by": "Ordre"},
         {"name": "Ordre", "type": "int64", "hidden": True, "format": "0"}],
        force=force)

    measures = [{
        "name": "Selected theme",
        "dax": "SELECTEDVALUE ( '%s'[%s], \"%s\" )"
               % (theme_table, theme_table, default),
        "display_folder": THEME_FOLDER,
        "description": "Which side of the palette every Color measure reads. "
                       "Falls back to %s when the slicer is cleared." % default,
    }]
    for token in SVG_TOKENS:
        measures.append({
            "name": "Color %s" % token,
            "dax": 'IF ( [Selected theme] = "Dark", "%s", "%s" )'
                   % (dark[token], light[token]),
            "display_folder": THEME_FOLDER,
            "description": SVG_TOKENS[token],
        })
    model.add_measures(table, measures, force=force)
    return [m["name"] for m in measures]


def add_svg_measure(model, table: str, name: str, card: SvgCard,
                    display_folder: str = SVG_FOLDER, description: str = None,
                    force: bool = False) -> SvgCard:
    """Write one composed primitive into the model with the dataCategory that
    makes a card render it as a picture. Returns the card, so the call can be
    threaded straight into add_svg_card:

        card = add_svg_measure(model, "_Measures", "KPI hires (SVG)",
                               kpi_sparkbar(348, 119, ...))
        page.add_svg_card("_Measures", "KPI hires (SVG)", 40, 100,
                          card.width, card.height)
    """
    model.add_measure(table, name, card.dax, text_measure=True,
                      data_category="ImageUrl", display_folder=display_folder,
                      description=description, force=force)
    return card
