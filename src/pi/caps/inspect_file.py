"""`file-inspect`: look inside files that `read` shows as noise (Oh My Pi's read for SQLite, archives, notebooks).

One read-only tool, `inspect`:

    .sqlite .db .sqlite3     tables with their columns and row counts; `table` + `limit` shows rows; `query` runs a
                             SELECT (the database is opened read-only)
    .zip .tar .tgz .tar.gz   the member list with sizes; `member` prints one text file from inside
    .ipynb                   the notebook as readable cells: markdown, code, and text outputs (images are noted)
    .csv .tsv                the header, the number of rows, and the first rows

Everything is standard library. PDF and Office files are not covered.
"""
from __future__ import annotations

import csv
import io
import json
import os
import sqlite3
import tarfile
import zipfile
from typing import Any, Dict, List

from ..tools.base import ToolDef, ToolError, check_abort, resolve_to_cwd, text_result
from . import Capability

MAX_CHARS, MAX_MEMBERS, MAX_ROWS = 20_000, 300, 200


def _clip(text: str) -> str:
    return text if len(text) <= MAX_CHARS else text[:MAX_CHARS] + f"\n[{len(text) - MAX_CHARS} more characters]"


def _cell(v: Any) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, bytes):
        return f"<{len(v)} bytes>"
    s = str(v).replace("\n", "\\n")
    return s if len(s) <= 80 else s[:77] + "..."


def _rows(cols: List[str], rows: List[Any]) -> str:
    return " | ".join(cols) + "\n" + "\n".join(" | ".join(_cell(v) for v in r) for r in rows)


def inspect_sqlite(path: str, p: Dict[str, Any]) -> str:
    uri = "file:" + path.replace("\\", "/").replace("?", "%3f").replace("#", "%23") + "?mode=ro"
    try:
        con = sqlite3.connect(uri, uri=True, timeout=5)
    except sqlite3.Error as e:
        raise ToolError(f"cannot open database: {e}") from None
    try:
        limit = max(1, min(int(p.get("limit") or 20), MAX_ROWS))
        if p.get("query"):
            q = str(p["query"]).strip().rstrip(";")
            if not q.lower().startswith(("select", "with", "pragma", "explain")):
                raise ToolError("only SELECT / WITH / PRAGMA / EXPLAIN queries are allowed")
            cur = con.execute(q)
            cols = [d[0] for d in cur.description or []]
            rows = cur.fetchmany(limit)
            return _rows(cols, rows) + (f"\n[first {limit} rows]" if len(rows) == limit else "")
        if p.get("table"):
            t = str(p["table"])
            if not con.execute("SELECT 1 FROM sqlite_master WHERE name = ?", (t,)).fetchone():
                raise ToolError(f"no table '{t}'")
            cur = con.execute(f'SELECT * FROM "{t}" LIMIT ?', (limit,))
            return _rows([d[0] for d in cur.description], cur.fetchall())
        out = []
        for name, kind in con.execute("SELECT name, type FROM sqlite_master WHERE type IN ('table','view') AND name NOT LIKE 'sqlite_%' ORDER BY name"):
            cols = [f"{c[1]} {c[2]}".strip() for c in con.execute(f'PRAGMA table_info("{name}")')]
            try:
                count = con.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
            except sqlite3.Error:
                count = "?"
            out.append(f"{kind} {name} ({count} rows)\n  " + ", ".join(cols))
        return "\n".join(out) or "(no tables)"
    except sqlite3.Error as e:
        raise ToolError(f"sqlite: {e}") from None
    finally:
        con.close()


def inspect_archive(path: str, p: Dict[str, Any]) -> str:
    member = str(p.get("member") or "")
    try:
        if zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as z:
                if member:
                    try:
                        data = z.read(member)[:MAX_CHARS * 4]
                    except KeyError:
                        raise ToolError(f"no member '{member}'") from None
                    return _clip(data.decode("utf-8", "replace"))
                infos = z.infolist()
                rows = [f"{i.file_size:>10}  {i.filename}" for i in infos[:MAX_MEMBERS]]
                return f"{len(infos)} entries, {sum(i.file_size for i in infos):,} bytes unpacked\n" + "\n".join(rows) + \
                    (f"\n[{len(infos) - MAX_MEMBERS} more]" if len(infos) > MAX_MEMBERS else "")
        with tarfile.open(path) as t:
            if member:
                try:
                    f = t.extractfile(member)
                except KeyError:
                    raise ToolError(f"no member '{member}'") from None
                if f is None:
                    raise ToolError(f"'{member}' is not a regular file")
                return _clip(f.read(MAX_CHARS * 4).decode("utf-8", "replace"))
            members = t.getmembers()
            rows = [f"{m.size:>10}  {m.name}{'/' if m.isdir() else ''}" for m in members[:MAX_MEMBERS]]
            return f"{len(members)} entries\n" + "\n".join(rows) + (f"\n[{len(members) - MAX_MEMBERS} more]" if len(members) > MAX_MEMBERS else "")
    except (tarfile.TarError, zipfile.BadZipFile, OSError) as e:
        raise ToolError(f"cannot read archive: {e}") from None


def inspect_notebook(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as f:
            nb = json.loads(f.read())
    except (OSError, ValueError) as e:
        raise ToolError(f"cannot read notebook: {e}") from None
    out = []
    for i, cell in enumerate(nb.get("cells") or [], 1):
        src = "".join(cell.get("source") or []) if isinstance(cell.get("source"), list) else str(cell.get("source") or "")
        kind = cell.get("cell_type")
        out.append(f"--- [{i}] {kind} ---\n{src.rstrip()}")
        for o in cell.get("outputs") or []:
            if o.get("output_type") == "stream":
                out.append("[output]\n" + "".join(o.get("text") or [])[:1500].rstrip())
            elif o.get("output_type") == "error":
                out.append(f"[error] {o.get('ename')}: {o.get('evalue')}")
            else:
                data = o.get("data") or {}
                if "text/plain" in data:
                    out.append("[result]\n" + "".join(data["text/plain"])[:1500].rstrip())
                images = [k for k in data if k.startswith("image/")]
                if images:
                    out.append(f"[{', '.join(images)} output not shown]")
    return "\n".join(out) or "(empty notebook)"


def inspect_csv(path: str, p: Dict[str, Any]) -> str:
    limit = max(1, min(int(p.get("limit") or 15), MAX_ROWS))
    try:
        with open(path, encoding="utf-8-sig", errors="replace", newline="") as f:
            sample = f.read(8192)
            f.seek(0)
            try:
                dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
            except csv.Error:
                dialect = csv.excel_tab if path.lower().endswith(".tsv") else csv.excel
            reader = csv.reader(f, dialect)
            rows, total = [], 0
            for row in reader:
                total += 1
                if len(rows) <= limit:
                    rows.append(row)
    except OSError as e:
        raise ToolError(f"cannot read: {e}") from None
    if not rows:
        return "(empty file)"
    return f"{total - 1} data rows, {len(rows[0])} columns\n" + _rows(rows[0], rows[1:limit + 1])


def _tools(session: Any) -> Dict[str, ToolDef]:
    def execute(_id, p, signal=None, on_update=None, ctx=None):
        check_abort(signal)
        if not p.get("path"):
            raise ToolError("path is required")
        path = resolve_to_cwd(str(p["path"]), session.cwd)
        if not os.path.isfile(path):
            raise ToolError(f"{p['path']} does not exist")
        low = path.lower()
        with open(path, "rb") as f:
            head = f.read(16)
        if head.startswith(b"SQLite format 3") or low.endswith((".sqlite", ".sqlite3", ".db")):
            kind, text = "sqlite", inspect_sqlite(path, p)
        elif low.endswith(".ipynb"):
            kind, text = "notebook", inspect_notebook(path)
        elif low.endswith((".csv", ".tsv")):
            kind, text = "csv", inspect_csv(path, p)
        elif zipfile.is_zipfile(path) or low.endswith((".tar", ".tgz", ".tar.gz", ".tar.bz2", ".tar.xz", ".txz")) or tarfile.is_tarfile(path):
            kind, text = "archive", inspect_archive(path, p)
        else:
            raise ToolError("inspect handles SQLite databases, zip / tar archives, .ipynb notebooks and .csv / .tsv files; use read for text")
        return text_result(_clip(text), {"kind": kind})

    return {"inspect": ToolDef(
        name="inspect", label="Inspect",
        description="Look inside a SQLite database (tables; `table` for rows; `query` for a SELECT), a zip / tar archive "
                    "(member list; `member` to print one file), a Jupyter notebook (cells and outputs as text) or a CSV "
                    "(shape and first rows). Use it instead of read for these formats.",
        parameters={"type": "object", "properties": {
            "path": {"type": "string"}, "table": {"type": "string"}, "query": {"type": "string"}, "member": {"type": "string"},
            "limit": {"type": "integer"}}, "required": ["path"]},
        execute=execute, prompt_snippet="Inspect databases, archives, notebooks and CSV files", read_only=True)}


CAPABILITIES = [Capability("file-inspect", "File inspector", "inspect: SQLite databases, zip / tar archives, notebooks and CSV "
                           "files as readable text.", "Oh My Pi read for SQLite / archives / notebooks", tools=_tools)]
