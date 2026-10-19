"""
tmdl_editor.py — create and edit DAX measures directly in .SemanticModel TMDL files.

This replaces the SemanticOps-MCP authoring path: measures are written to
<project>.SemanticModel/definition/tables/<table>.tmdl while Power BI Desktop
is CLOSED. PBI reads them on next open — no Ctrl+S, no "PBI must be open at
session start" constraint, no reopen dance when the plan changes.

SemanticOps MCP remains useful for VERIFICATION (running DAX queries against a
live PBI instance), but is no longer required to author measures.

Usage:
    import sys; sys.path.insert(0, r"C:\\Users\\valer\\.claude\\mcp_servers")
    from tmdl_editor import TmdlModel

    m = TmdlModel(r"C:\\Users\\valer\\portfolio\\proj\\proj.SemanticModel")
    print(m.summary())

    m.add_measure("sales", "Total Revenue", "SUM(sales[revenue])",
                  format_string="$#,0")
    m.add_measure("sales", "Conversion Rate",
                  "DIVIDE([Total Purchases], [Total Sessions], 0)",
                  format_string="0.0%")

RULE: every measure gets a format_string — raw 1234567.8901 on a KPI card is
the most visible quality defect a dashboard can have. Common formats:
    "#,0"        -> 12,346          (counts)
    "$#,0"       -> $12,346         (currency)
    "$#,0,.0K"   -> $12.3K          (currency, thousands)
    "$#,0,,.1M"  -> $12.3M          (currency, millions)
    "0.0%"       -> 12.3%           (rates)
    "0.00"       -> 12.35           (decimals)
"""

import os, re, uuid, subprocess

__version__ = "1.0"


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


def _quote(name: str) -> str:
    """Quote a TMDL object name when needed."""
    if re.fullmatch(r"[A-Za-z0-9_]+", name):
        return name
    return "'" + name.replace("'", "''") + "'"


def _parse_decl(line: str, keyword: str):
    """Parse `keyword Name` / `keyword 'Quoted Name'` declarations."""
    s = line.strip()
    if not (s == keyword or s.startswith(keyword + " ")):
        return None
    rest = s[len(keyword):].strip()
    if not rest:
        return None
    if rest.startswith("'"):
        end = rest.find("'", 1)
        while end != -1 and end + 1 < len(rest) and rest[end + 1] == "'":
            end = rest.find("'", end + 2)
        return rest[1:end].replace("''", "'") if end != -1 else None
    for i, ch in enumerate(rest):
        if ch in (" ", "\t", "="):
            return rest[:i]
    return rest


class TmdlTableFile:
    """One table .tmdl file. Preserves untouched lines byte-for-byte."""

    def __init__(self, path: str):
        self.path = path
        with open(path, "rb") as f:
            raw = f.read()
        self.bom = raw.startswith(b"\xef\xbb\xbf")
        text = raw.decode("utf-8-sig")
        self.eol = "\r\n" if "\r\n" in text else "\n"
        # splitlines keeps structure; we re-join with the detected EOL
        self.lines = text.split(self.eol) if self.eol in text else text.splitlines()
        self.table_name = None
        for ln in self.lines:
            if ln.startswith("table"):
                self.table_name = _parse_decl(ln, "table")
                break

    # ── block parsing ──────────────────────────────────────────────────────────

    def _block_ranges(self, keyword: str) -> list:
        """
        [(name, start_idx, end_idx_exclusive), ...] for `\\t<keyword> ...` blocks.
        A block ends at the next line with indentation <= 1 tab (non-blank).
        Trailing blank lines belong to the block.
        """
        out = []
        i = 0
        n = len(self.lines)
        while i < n:
            line = self.lines[i]
            if line.startswith("\t" + keyword + " "):
                name = _parse_decl(line, keyword)
                j = i + 1
                while j < n:
                    nxt = self.lines[j]
                    if nxt.strip() == "":
                        j += 1
                        continue
                    if not nxt.startswith("\t\t"):
                        break
                    j += 1
                out.append((name, i, j))
                i = j
            else:
                i += 1
        return out

    def measures(self) -> list:
        return [name for name, _, _ in self._block_ranges("measure")]

    def columns(self) -> list:
        return [name for name, _, _ in self._block_ranges("column")]

    def measure_block(self, name: str):
        for n, s, e in self._block_ranges("measure"):
            if n == name:
                return s, e
        return None

    # ── editing ────────────────────────────────────────────────────────────────

    def _insertion_point(self) -> int:
        """After the last measure block; else before the first column/partition."""
        mblocks = self._block_ranges("measure")
        if mblocks:
            return mblocks[-1][2]
        for i, line in enumerate(self.lines):
            if line.startswith("\tcolumn ") or line.startswith("\tpartition "):
                return i
        return len(self.lines)

    def build_measure_lines(self, name: str, dax: str, format_string: str = None,
                            display_folder: str = None, description: str = None,
                            lineage_tag: str = None,
                            data_category: str = None) -> list:
        lines = []
        if description:
            for d in str(description).split("\n"):
                lines.append(f"\t/// {d}")
        dax = dax.strip()
        if "\n" in dax:
            lines.append(f"\tmeasure {_quote(name)} =")
            for dl in dax.split("\n"):
                lines.append("\t\t\t" + dl.rstrip())
        else:
            lines.append(f"\tmeasure {_quote(name)} = {dax}")
        if format_string:
            lines.append(f"\t\tformatString: {format_string}")
        lines.append(f"\t\tlineageTag: {lineage_tag or uuid.uuid4()}")
        if display_folder:
            lines.append(f"\t\tdisplayFolder: {display_folder}")
        if data_category:
            # last in the block, where Power BI itself writes it
            lines.append(f"\t\tdataCategory: {data_category}")
        lines.append("")
        return lines

    def upsert_measure(self, name: str, dax: str, format_string: str = None,
                       display_folder: str = None, description: str = None,
                       data_category: str = None) -> str:
        block = self.measure_block(name)
        # preserve the existing lineageTag on update — external tools track
        # objects by it; regenerating one per edit churns diffs for nothing
        existing_tag = None
        if block:
            for i in range(block[0], block[1]):
                stripped = self.lines[i].strip()
                if stripped.startswith("lineageTag:"):
                    existing_tag = stripped.split(":", 1)[1].strip()
                    break
        new_lines = self.build_measure_lines(name, dax, format_string,
                                             display_folder, description,
                                             lineage_tag=existing_tag,
                                             data_category=data_category)
        if block:
            s, e = block
            # a measure's /// description sits ABOVE the `measure` line, so it
            # falls outside the block range — swallow it, or every upsert of a
            # documented measure stacks another copy of the comment on top of
            # the last one.
            while s > 0 and self.lines[s - 1].lstrip().startswith("///"):
                s -= 1
            self.lines[s:e] = new_lines
            return "updated"
        ip = self._insertion_point()
        self.lines[ip:ip] = new_lines
        return "created"

    def remove_measure(self, name: str) -> bool:
        block = self.measure_block(name)
        if not block:
            return False
        s, e = block
        del self.lines[s:e]
        return True

    def save(self) -> None:
        text = self.eol.join(self.lines)
        data = text.encode("utf-8")
        if self.bom:
            data = b"\xef\xbb\xbf" + data
        with open(self.path, "wb") as f:
            f.write(data)


class TmdlModel:
    """
    Edits measures across a .SemanticModel folder.

    guard_pbi=True (default): every write raises if PBIDesktop.exe is running —
    PBI holds the model in memory and will overwrite your edit on its next save.
    """

    def __init__(self, semantic_model_folder: str, guard_pbi: bool = True):
        self.folder = semantic_model_folder
        self.tables_dir = os.path.join(semantic_model_folder, "definition", "tables")
        if not os.path.isdir(self.tables_dir):
            raise FileNotFoundError(
                f"Tables folder not found: {self.tables_dir}\n"
                "Point TmdlModel at the .SemanticModel folder of a saved .pbip project."
            )
        self.guard_pbi = guard_pbi

    # ── lookups ────────────────────────────────────────────────────────────────

    def _files(self) -> list:
        return [os.path.join(self.tables_dir, f)
                for f in sorted(os.listdir(self.tables_dir)) if f.endswith(".tmdl")]

    def _table_file(self, table: str) -> TmdlTableFile:
        for path in self._files():
            tf = TmdlTableFile(path)
            if tf.table_name == table:
                return tf
        names = self.list_tables()
        raise KeyError(f"Table '{table}' not found. Available: {names}")

    def list_tables(self, include_hidden: bool = False) -> list:
        names = []
        for path in self._files():
            t = TmdlTableFile(path).table_name
            if t and (include_hidden or not t.startswith(("LocalDateTable_", "DateTableTemplate_"))):
                names.append(t)
        return names

    def list_columns(self, table: str) -> list:
        return self._table_file(table).columns()

    def list_measures(self, table: str) -> list:
        return self._table_file(table).measures()

    # ── editing ────────────────────────────────────────────────────────────────

    def _guard(self, force: bool):
        if self.guard_pbi and not force and powerbi_is_open():
            raise RuntimeError(
                "Power BI Desktop is running — close it before editing .SemanticModel/ "
                "(PBI will overwrite your edit on its next save). "
                "Use force=True to override."
            )

    def add_measure(self, table: str, name: str, dax: str,
                    format_string: str = None, display_folder: str = None,
                    description: str = None, force: bool = False,
                    text_measure: bool = False,
                    data_category: str = None) -> None:
        """
        Create or update a measure. ALWAYS pass format_string (see module
        docstring for common formats) — unformatted KPI numbers look broken.
        text_measure=True: the measure returns text or a hex color (labels,
        conditional-format colors) — no format string applies, no warning.

        data_category="ImageUrl": the measure returns an image reference rather
        than a value — a URL, or an SVG document as a data URI (svg_measures).
        Without it a cardVisual bound to the measure renders the raw string
        instead of the picture, and nothing reports an error. Implies
        text_measure.
        """
        self._guard(force)
        if data_category:
            text_measure = True
        if not format_string and not text_measure:
            print(f"  [warn] measure '{name}' has no format_string — "
                  "KPI cards will show raw unformatted numbers")
        tf = self._table_file(table)
        action = tf.upsert_measure(name, dax, format_string, display_folder,
                                   description, data_category)
        tf.save()
        print(f"  [{table}] {action} measure '{name}'"
              + (f"  ({format_string})" if format_string else "")
              + (f"  [{data_category}]" if data_category else ""))

    def add_measures(self, table: str, measures: list, force: bool = False) -> None:
        """
        Batch create/update.
        measures: [{"name": ..., "dax": ..., "format_string": ...,
                    "data_category": ...}, ...]
        """
        self._guard(force)
        tf = self._table_file(table)
        for m in measures:
            action = tf.upsert_measure(
                m["name"], m["dax"], m.get("format_string"),
                m.get("display_folder"), m.get("description"),
                m.get("data_category"))
            print(f"  [{table}] {action} measure '{m['name']}'"
                  + (f"  ({m.get('format_string')})" if m.get("format_string") else ""))
        tf.save()

    def remove_measure(self, table: str, name: str, force: bool = False) -> bool:
        self._guard(force)
        tf = self._table_file(table)
        ok = tf.remove_measure(name)
        if ok:
            tf.save()
            print(f"  [{table}] removed measure '{name}'")
        else:
            print(f"  [{table}] measure '{name}' not found")
        return ok

    # ── calculated tables ──────────────────────────────────────────────────────

    _DTYPES = {"string": "string", "int64": "int64", "int": "int64",
               "double": "double", "decimal": "decimal", "boolean": "boolean",
               "dateTime": "dateTime", "date": "dateTime"}

    def _model_path(self) -> str:
        return os.path.join(self.folder, "definition", "model.tmdl")

    def add_calculated_table(self, name: str, dax: str, columns: list,
                             force: bool = False) -> str:
        """
        Create (or replace) a DAX calculated table and reference it from
        model.tmdl, so Power BI loads it with the rest of the model.

        A disconnected one-column table is how a report gets a switch that no
        data feeds — the Light/Dark toggle, a period picker, a metric chooser:
        a slicer binds to the column and a measure reads it back with
        SELECTEDVALUE.

            m.add_calculated_table(
                "Theme",
                'DATATABLE ( "Theme", STRING, "Ordre", INTEGER, '
                '{ { "Light", 1 }, { "Dark", 2 } } )',
                [{"name": "Theme", "type": "string", "sort_by": "Ordre"},
                 {"name": "Ordre", "type": "int64", "hidden": True,
                  "format": "0"}])

        columns: [{"name", "type", "hidden", "format", "sort_by"}] — these must
        match the DATATABLE's own column names and order, because TMDL does not
        derive them from the expression.
        """
        self._guard(force)
        if not columns:
            raise ValueError("a calculated table needs its columns declared")
        path = os.path.join(self.tables_dir, f"{name}.tmdl")
        # keep the table's identity across rebuilds — external tools track it
        tag = None
        if os.path.exists(path):
            for line in open(path, encoding="utf-8-sig"):
                if line.strip().startswith("lineageTag:"):
                    tag = line.split(":", 1)[1].strip()
                    break
        L = [f"table {_quote(name)}", f"\tlineageTag: {tag or uuid.uuid4()}", ""]
        for c in columns:
            dtype = self._DTYPES.get(c.get("type", "string"))
            if dtype is None:
                raise ValueError(f"column '{c['name']}': unknown type "
                                 f"{c.get('type')!r}, choose from {sorted(self._DTYPES)}")
            L.append(f"\tcolumn {_quote(c['name'])}")
            L.append(f"\t\tdataType: {dtype}")
            if c.get("hidden"):
                L.append("\t\tisHidden")
            if c.get("format"):
                L.append(f"\t\tformatString: {c['format']}")
            L.append(f"\t\tlineageTag: {uuid.uuid4()}")
            L.append("\t\tsummarizeBy: none")
            # calculated-table columns name their source in brackets; an
            # unbracketed sourceColumn is the M-partition spelling and makes
            # the table fail to load
            L.append(f"\t\tsourceColumn: [{c['name']}]")
            if c.get("sort_by"):
                L.append(f"\t\tsortByColumn: {_quote(c['sort_by'])}")
            L.append("")
            L.append("\t\tannotation SummarizationSetBy = Automatic")
            L.append("")
        L.append(f"\tpartition {_quote(name)} = calculated")
        L.append("\t\tmode: import")
        L.append(f"\t\tsource = {dax.strip()}")
        L.append("")
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write("\r\n".join(L))

        self._ref_table(name)
        print(f"  calculated table '{name}' ({len(columns)} column(s)) -> {path}")
        return path

    def _ref_table(self, name: str) -> None:
        """Add `ref table <name>` to model.tmdl if it is not already there.
        Without the ref the .tmdl file sits in the folder and is ignored."""
        path = self._model_path()
        if not os.path.exists(path):
            print(f"  [warn] {path} not found — add 'ref table {name}' yourself")
            return
        text = open(path, encoding="utf-8-sig").read()
        ref = f"ref table {_quote(name)}"
        if re.search(r"(?m)^%s\s*$" % re.escape(ref), text):
            return
        eol = "\r\n" if "\r\n" in text else "\n"
        lines = text.split(eol)
        # before `ref cultureInfo`, which Power BI writes last
        at = next((i for i, l in enumerate(lines)
                   if l.startswith("ref cultureInfo")), len(lines))
        lines[at:at] = [ref, ""]
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(eol.join(lines))

    # ── relationships ──────────────────────────────────────────────────────────

    def _rel_path(self) -> str:
        return os.path.join(self.folder, "definition", "relationships.tmdl")

    def _col_ref(self, table: str, column: str) -> str:
        return f"{_quote(table)}.{_quote(column)}"

    def list_relationships(self) -> list:
        """[(from 'Table.Col', to 'Table.Col'), ...] from relationships.tmdl."""
        path = self._rel_path()
        if not os.path.exists(path):
            return []
        out, frm = [], None
        for line in open(path, encoding="utf-8-sig").read().splitlines():
            s = line.strip()
            if s.startswith("fromColumn:"):
                frm = s.split(":", 1)[1].strip()
            elif s.startswith("toColumn:") and frm is not None:
                out.append((frm, s.split(":", 1)[1].strip()))
                frm = None
        return out

    def add_relationship(self, from_table: str, from_column: str,
                         to_table: str, to_column: str,
                         cross_filter_both: bool = False, active: bool = True,
                         force: bool = False) -> None:
        """
        Create a many-to-one relationship (fact -> dimension) in
        definition/relationships.tmdl. PBI loads the file automatically.

        from_table/from_column: the MANY side (fact table's foreign key)
        to_table/to_column:     the ONE side (dimension key — must be unique)
        cross_filter_both:      bidirectional cross-filtering (default single)
        Duplicate (same from/to columns) is skipped with a notice.
        """
        self._guard(force)
        frm = self._col_ref(from_table, from_column)
        to = self._col_ref(to_table, to_column)
        if (frm, to) in self.list_relationships():
            print(f"  relationship {frm} -> {to} already exists — skipped")
            return
        lines = [f"relationship {uuid.uuid4()}"]
        if not active:
            lines.append("\tisActive: false")
        if cross_filter_both:
            lines.append("\tcrossFilteringBehavior: bothDirections")
        lines.append(f"\tfromColumn: {frm}")
        lines.append(f"\ttoColumn: {to}")
        lines.append("")
        path = self._rel_path()
        existing = ""
        if os.path.exists(path):
            existing = open(path, encoding="utf-8-sig").read()
            if existing and not existing.endswith("\n"):
                existing += "\r\n"
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(existing + "\r\n".join(lines) + "\r\n")
        print(f"  relationship: {frm} (many) -> {to} (one)"
              + (" [both directions]" if cross_filter_both else ""))

    def set_column_format(self, table: str, column: str, format_string: str,
                          force: bool = False) -> None:
        """Set/replace formatString on a column (e.g. a date column)."""
        self._guard(force)
        tf = self._table_file(table)
        for name, s, e in tf._block_ranges("column"):
            if name != column:
                continue
            for i in range(s + 1, e):
                if tf.lines[i].startswith("\t\tformatString:"):
                    tf.lines[i] = f"\t\tformatString: {format_string}"
                    tf.save()
                    return
            tf.lines.insert(s + 1, f"\t\tformatString: {format_string}")
            tf.save()
            return
        raise KeyError(f"Column '{column}' not found in table '{table}'. "
                       f"Available: {tf.columns()}")

    # ── reporting ──────────────────────────────────────────────────────────────

    def summary(self) -> str:
        lines = [f"Semantic model: {self.folder}"]
        for t in self.list_tables():
            tf = self._table_file(t)
            ms = tf.measures()
            lines.append(f"  table {t}: {len(tf.columns())} columns, {len(ms)} measures")
            for m in ms:
                lines.append(f"    measure: {m}")
        return "\n".join(lines)
