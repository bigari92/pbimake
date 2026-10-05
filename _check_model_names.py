# Catch TMDL name collisions BEFORE Power BI Desktop refuses to open the project.
#
# The failure this exists for:
#
#     Issues were found — There's a problem with the definition content in your
#     Power BI Project. The 'Hires' measure cannot be created because a column
#     with the same name already exists.
#
# In Tabular, a table's columns, measures and hierarchies share ONE namespace and
# it is CASE-INSENSITIVE. So `measure Hires` beside `column hires` is fatal — and
# it is easy to write, because the natural display name for a measure is the
# Title Case of the raw feed column it aggregates. Measure names must also be
# unique across the WHOLE model, not just their table.
#
# Power BI only reports these on open, one at a time, after the model is written.
# This finds all of them in a second, offline.
#
# Usage — accepts .SemanticModel folders, .pbip project folders, or a parent
# folder to sweep:
#     python _check_model_names.py <path> [more paths...]
#     python _check_model_names.py C:\...\sales\sales.SemanticModel
#     python _check_model_names.py C:\...\dashboards          # every project under it
#
# Exit code 1 if any FATAL is found, so it can gate a build.

import glob
import os
import re
import sys

sys.stdout.reconfigure(encoding="utf-8")

# TMDL: one indented declaration per line; names are bare or 'single quoted'
DECL = re.compile(r"^\s+(measure|column|hierarchy)\s+('([^']*)'|[^\s=]+)\s*(=|$)")


def parse_table(path):
    """(table_name, {kind: [names]}) from one .tmdl table file."""
    table = os.path.splitext(os.path.basename(path))[0]
    found = {"measure": [], "column": [], "hierarchy": []}
    with open(path, encoding="utf-8-sig") as f:
        for line in f:
            stripped = line.lstrip()
            if stripped.startswith("table ") or stripped.startswith("///"):
                m = re.match(r"^table\s+('([^']*)'|\S+)", line.strip())
                if m:
                    table = m.group(2) or m.group(1)
                continue
            m = DECL.match(line)
            if m:
                found[m.group(1)].append(m.group(3) or m.group(2))
    return table, found


def check_model(model_dir):
    """[(severity, message)] for one .SemanticModel folder."""
    problems = []
    tables = {}
    for f in sorted(glob.glob(os.path.join(model_dir, "definition", "tables",
                                           "*.tmdl"))):
        name, found = parse_table(f)
        tables[name] = (found, os.path.relpath(f, model_dir))

    all_measures = {}          # lower name -> [(table, original, relpath)]
    columns_by_table = {}      # table -> {lower: original}

    for table, (found, rel) in tables.items():
        cols = {}
        for kind in ("column", "hierarchy"):
            for n in found[kind]:
                key = n.lower()
                if key in cols:
                    problems.append(("FATAL", "%s: '%s' declared twice in table "
                                     "'%s'" % (rel, n, table)))
                cols[key] = n
        columns_by_table[table] = cols

        for n in found["measure"]:
            key = n.lower()
            if key in cols:
                problems.append((
                    "FATAL",
                    "%s: measure '%s' collides with column '%s' in its own "
                    "table '%s' — Power BI will refuse to open the project"
                    % (rel, n, cols[key], table)))
            all_measures.setdefault(key, []).append((table, n, rel))

    for key, defs in all_measures.items():
        if len(defs) > 1:
            where = ", ".join("%s (%s)" % (t, r) for t, _n, r in defs)
            problems.append(("FATAL", "measure '%s' defined %d times — measure "
                             "names must be unique across the whole model: %s"
                             % (defs[0][1], len(defs), where)))

    for key, defs in all_measures.items():
        for table, cols in columns_by_table.items():
            if key in cols and all(t != table for t, _n, _r in defs):
                problems.append(("WARN", "measure '%s' has the same name as "
                                 "column '%s' in table '%s' — legal, but [%s] "
                                 "and %s[%s] read alike"
                                 % (defs[0][1], cols[key], table, defs[0][1],
                                    table, cols[key])))
    return problems


def resolve(path):
    """Any of: a .SemanticModel folder, a project folder, a parent to sweep."""
    path = os.path.abspath(path)
    if path.endswith(".SemanticModel") and os.path.isdir(path):
        return [path]
    direct = sorted(glob.glob(os.path.join(path, "*.SemanticModel")))
    if direct:
        return direct
    return sorted(glob.glob(os.path.join(path, "*", "*.SemanticModel")))


def main():
    args = sys.argv[1:] or ["."]
    models = []
    for a in args:
        models += resolve(a)
    if not models:
        print("no .SemanticModel folder found under: %s" % ", ".join(args))
        return 1

    fatal = warn = 0
    for m in models:
        problems = check_model(m)
        if not problems:
            continue
        print("== %s" % m)
        for sev, msg in problems:
            print("  %-5s %s" % (sev, msg))
            if sev == "FATAL":
                fatal += 1
            else:
                warn += 1

    if fatal:
        print("\n%d model(s) — %d FATAL, %d warning. Fix the fatals before "
              "opening Power BI." % (len(models), fatal, warn))
    else:
        print("\nNAMES OK — %d model(s), %d warning(s)" % (len(models), warn))
    return 1 if fatal else 0


if __name__ == "__main__":
    sys.exit(main())
