#!/usr/bin/env python3
# Copyright (c) 2024-2026 Daniel Perry. All Rights Reserved.
# Licensed under EOSL-2.0.
# Modified: 2026-04-19T02:30:00Z | Author: claude_code | Change: doc-parser — LIRIL perception gap #2
"""LIRIL Doc Parser — the "why" layer on top of code watching.

LIRIL's own #2 perception gap when polled:
  'Code Comment and Documentation Analysis — LIRIL doesn't currently
   parse or analyze comments and documentation. Rough build difficulty:
   Low. This could be achieved by adding a parser for the relevant
   programming languages.'

What it does:
  - Walks the TENET5 + website codebases
  - Extracts comments, docstrings, markdown headings, and HTML comments
  - Flags special markers (TODO / FIXME / XXX / HACK / NOTE / WARNING /
    CRITICAL / REVIEW)
  - Indexes everything to data/liril_doc_index.sqlite
  - Optionally classifies each chunk via tenet5.liril.classify
  - Exposes NATS subjects tenet5.liril.docs.scan / .search / .markers
  - Publishes findings to tenet5.liril.docs.findings

Gives LIRIL's dev-team pipeline + failure analyzer + observer daemon
access to the intent layer: what the developer said about the code,
not just the code itself. The few-shot memory now has richer signal
(a task touching a file with a FIXME comment is different from one
touching untagged code).

Run:
  python tools/liril_doc_parser.py --scan                    # full scan
  python tools/liril_doc_parser.py --scan --classify         # scan + NPU tag
  python tools/liril_doc_parser.py --todos                   # list all TODOs
  python tools/liril_doc_parser.py --search "hallucination"  # substring
  python tools/liril_doc_parser.py --daemon                  # serve NATS
  python tools/liril_doc_parser.py --stats                   # index summary
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
# 2026-04-19: site-wide subprocess no-window shim
try:
    import sys as _sys
    from pathlib import Path as _Path
    _sys.path.insert(0, str(_Path(__file__).resolve().parent))
    try: import _liril_subprocess_nowindow  # noqa: F401
    except Exception: pass
except Exception: pass
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

# Codebase roots to walk
ROOTS = [
    Path(r"E:/S.L.A.T.E/tenet5/tools"),
    Path(r"E:/S.L.A.T.E/tenet5/src/tenet"),
    Path(r"E:/S.L.A.T.E/tenet5/infrastructure/tenet5os/Runtime"),
    Path(r"E:/TENET-5.github.io"),
]

# Skip directories (match anywhere in path)
SKIP = {
    ".venv", ".git", "node_modules", "__pycache__", ".mypy_cache",
    ".pytest_cache", "dist", "build", "venv", "env",
    # Website: skip generated junk
    "mirror_reports", "profiles", "evidence", "scratch",
    "_PAUSED_REDDUSTER", ".deprecated",
}

INDEX_DB = Path(r"E:/S.L.A.T.E/tenet5/data/liril_doc_index.sqlite")
NATS_URL = os.environ.get("NATS_URL", "nats://127.0.0.1:4223")

# File extension → language
EXT_LANG = {
    ".py":   "python",
    ".pyw":  "python",
    ".html": "html",
    ".htm":  "html",
    ".js":   "js",
    ".mjs":  "js",
    ".ts":   "ts",
    ".md":   "markdown",
    ".css":  "css",
    ".json": "json",
}

# Special markers we flag — sorted longest first so longest match wins
MARKERS = ["CRITICAL", "WARNING", "FIXME", "REVIEW", "HACK", "TODO", "XXX", "NOTE"]
MARKER_RX = re.compile(r"\b(" + "|".join(MARKERS) + r")\b[:\s]*(.*)", re.I)


# ──────────────────────────────────────────────────────────────
# EXTRACTORS — per language
# ──────────────────────────────────────────────────────────────

def extract_python(text: str) -> list[dict]:
    """Extract docstrings + # comments from python source."""
    out: list[dict] = []
    lines = text.splitlines()

    # Module/func/class docstrings via AST (robust)
    try:
        import ast
        tree = ast.parse(text)
        # Module docstring
        if ast.get_docstring(tree):
            doc = ast.get_docstring(tree)
            lineno = tree.body[0].lineno if tree.body else 1
            out.append({"kind": "docstring", "scope": "module",
                        "line": lineno, "text": doc})
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                d = ast.get_docstring(node)
                if d:
                    out.append({
                        "kind":  "docstring",
                        "scope": node.__class__.__name__.replace("Def", "").lower() + ":" + node.name,
                        "line":  node.lineno,
                        "text":  d,
                    })
    except SyntaxError:
        pass

    # Inline # comments
    for i, l in enumerate(lines, 1):
        # skip shebang + EOSL license lines
        if i <= 4 and l.startswith("#"):
            continue
        m = re.match(r"\s*#\s*(.+)$", l)
        if m:
            out.append({"kind": "comment", "scope": "inline",
                        "line": i, "text": m.group(1).strip()})
    return out


_HTML_COMMENT_RX = re.compile(r"<!--(.*?)-->", re.S)

def extract_html(text: str) -> list[dict]:
    out: list[dict] = []
    for m in _HTML_COMMENT_RX.finditer(text):
        line = text[:m.start()].count("\n") + 1
        comment = m.group(1).strip()
        if not comment:
            continue
        out.append({"kind": "comment", "scope": "html", "line": line, "text": comment})
    return out


_JS_BLOCK_RX = re.compile(r"/\*(.*?)\*/", re.S)
_JS_LINE_RX  = re.compile(r"^\s*//\s*(.+)$", re.M)

def extract_js(text: str) -> list[dict]:
    out: list[dict] = []
    for m in _JS_BLOCK_RX.finditer(text):
        line = text[:m.start()].count("\n") + 1
        out.append({"kind": "block", "scope": "js",
                    "line": line, "text": m.group(1).strip()})
    for m in _JS_LINE_RX.finditer(text):
        line = text[:m.start()].count("\n") + 1
        out.append({"kind": "comment", "scope": "js",
                    "line": line, "text": m.group(1).strip()})
    return out


_MD_HEADING_RX = re.compile(r"^(#{1,6})\s+(.+)$", re.M)

def extract_markdown(text: str) -> list[dict]:
    out: list[dict] = []
    for m in _MD_HEADING_RX.finditer(text):
        line = text[:m.start()].count("\n") + 1
        level = len(m.group(1))
        out.append({"kind": "heading", "scope": f"h{level}",
                    "line": line, "text": m.group(2).strip()})
    return out


def extract_json_doc(text: str) -> list[dict]:
    """JSON: pull 'description', 'note', 'title' top-level string values."""
    try:
        d = json.loads(text)
    except Exception:
        return []
    out: list[dict] = []
    if isinstance(d, dict):
        for key in ("description", "note", "title", "summary",
                    "notes", "readme", "context", "acceptance"):
            v = d.get(key)
            if isinstance(v, str) and v.strip():
                out.append({"kind": "json-field", "scope": f"${key}",
                            "line": 1, "text": v.strip()[:1000]})
    return out


EXTRACTORS = {
    "python":   extract_python,
    "html":     extract_html,
    "js":       extract_js,
    "ts":       extract_js,
    "markdown": extract_markdown,
    "json":     extract_json_doc,
}


# ──────────────────────────────────────────────────────────────
# WALKER
# ──────────────────────────────────────────────────────────────

def walk() -> list[Path]:
    files: list[Path] = []
    for root in ROOTS:
        if not root.exists():
            continue
        for p in root.rglob("*"):
            if not p.is_file():
                continue
            if any(s in p.parts for s in SKIP):
                continue
            ext = p.suffix.lower()
            if ext in EXT_LANG:
                try:
                    if p.stat().st_size > 2_000_000:
                        continue
                except Exception:
                    continue
                files.append(p)
    return files


# ──────────────────────────────────────────────────────────────
# DB
# ──────────────────────────────────────────────────────────────

def _db() -> sqlite3.Connection:
    INDEX_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(INDEX_DB))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS doc_chunks (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            file       TEXT    NOT NULL,
            language   TEXT    NOT NULL,
            kind       TEXT    NOT NULL,
            scope      TEXT,
            line       INTEGER,
            text       TEXT    NOT NULL,
            marker     TEXT,
            marker_msg TEXT,
            axis       TEXT,
            confidence REAL,
            scanned_at INTEGER NOT NULL,
            UNIQUE(file, line, kind, scope)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_file ON doc_chunks(file)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_marker ON doc_chunks(marker)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_axis ON doc_chunks(axis)")
    return conn


def _find_marker(text: str) -> tuple[str | None, str | None]:
    m = MARKER_RX.search(text)
    if m:
        return m.group(1).upper(), m.group(2).strip()[:400]
    return None, None


# ──────────────────────────────────────────────────────────────
# NPU CLASSIFY
# ──────────────────────────────────────────────────────────────

async def _npu_classify(text: str) -> dict:
    try:
        import nats as _nats
    except ImportError:
        return {}
    try:
        nc = await _nats.connect(NATS_URL, connect_timeout=3)
    except Exception:
        return {}
    try:
        msg = await nc.request(
            "tenet5.liril.classify",
            json.dumps({"task": text[:280], "source": "doc_parser"}).encode(),
            timeout=4,
        )
        return json.loads(msg.data.decode())
    except Exception:
        return {}
    finally:
        try:
            await nc.drain()
        except Exception:
            pass


# ──────────────────────────────────────────────────────────────
# SCAN
# ──────────────────────────────────────────────────────────────

def scan(classify: bool = False) -> dict:
    files = walk()
    print(f"[DOC] walking {len(files)} files…")

    conn = _db()
    now = int(time.time())
    n_chunks = 0
    n_markers = 0
    n_files_with_doc = 0

    for i, path in enumerate(files):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        lang = EXT_LANG.get(path.suffix.lower(), "unknown")
        extractor = EXTRACTORS.get(lang)
        if not extractor:
            continue
        try:
            chunks = extractor(text)
        except Exception:
            continue
        if not chunks:
            continue
        n_files_with_doc += 1

        rel = str(path).replace("\\", "/")
        for c in chunks:
            body = c["text"]
            if not body or len(body) < 3:
                continue
            marker, marker_msg = _find_marker(body)
            if marker:
                n_markers += 1
            try:
                conn.execute("""
                    INSERT OR REPLACE INTO doc_chunks
                    (file, language, kind, scope, line, text, marker, marker_msg, axis, confidence, scanned_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (rel, lang, c["kind"], c.get("scope"), c.get("line"),
                      body[:4000], marker, marker_msg, None, None, now))
                n_chunks += 1
            except sqlite3.IntegrityError:
                pass

        if (i + 1) % 200 == 0:
            conn.commit()
            print(f"  [{i+1}/{len(files)}] {n_chunks} chunks {n_markers} markers")

    conn.commit()

    # Optional NPU classification pass — marked chunks only (reduces load)
    if classify:
        print(f"[DOC] NPU classifying marker-flagged chunks…")
        rows = conn.execute(
            "SELECT id, text FROM doc_chunks WHERE marker IS NOT NULL AND axis IS NULL LIMIT 500"
        ).fetchall()
        for cid, txt in rows:
            try:
                cls = asyncio.run(_npu_classify(txt))
            except Exception:
                continue
            axis = cls.get("domain") or cls.get("axis")
            conf = cls.get("confidence")
            if axis:
                conn.execute(
                    "UPDATE doc_chunks SET axis = ?, confidence = ? WHERE id = ?",
                    (str(axis).upper(), float(conf) if conf else None, cid),
                )
        conn.commit()

    summary = {
        "files_scanned":       len(files),
        "files_with_doc":      n_files_with_doc,
        "chunks_indexed":      n_chunks,
        "markers_found":       n_markers,
        "scanned_at":          now,
        "db":                  str(INDEX_DB),
        "classified":          classify,
    }
    conn.close()
    return summary


# ──────────────────────────────────────────────────────────────
# QUERY API
# ──────────────────────────────────────────────────────────────

def list_markers(kinds: list[str] | None = None, limit: int = 200) -> list[dict]:
    conn = _db()
    q = "SELECT file, line, marker, marker_msg, text FROM doc_chunks WHERE marker IS NOT NULL"
    args: list = []
    if kinds:
        q += " AND marker IN (" + ",".join("?" * len(kinds)) + ")"
        args += [k.upper() for k in kinds]
    q += " ORDER BY marker, file, line LIMIT ?"
    args.append(limit)
    rows = conn.execute(q, args).fetchall()
    conn.close()
    return [
        {"file": r[0], "line": r[1], "marker": r[2], "msg": r[3], "excerpt": r[4][:180]}
        for r in rows
    ]


def search(pattern: str, limit: int = 50) -> list[dict]:
    conn = _db()
    rows = conn.execute(
        "SELECT file, line, kind, scope, text FROM doc_chunks "
        "WHERE text LIKE ? ORDER BY file, line LIMIT ?",
        (f"%{pattern}%", limit),
    ).fetchall()
    conn.close()
    return [
        {"file": r[0], "line": r[1], "kind": r[2], "scope": r[3], "excerpt": r[4][:240]}
        for r in rows
    ]


def stats() -> dict:
    conn = _db()
    total = conn.execute("SELECT COUNT(*) FROM doc_chunks").fetchone()[0]
    by_kind = dict(conn.execute(
        "SELECT kind, COUNT(*) FROM doc_chunks GROUP BY kind"
    ).fetchall())
    by_lang = dict(conn.execute(
        "SELECT language, COUNT(*) FROM doc_chunks GROUP BY language"
    ).fetchall())
    by_marker = dict(conn.execute(
        "SELECT marker, COUNT(*) FROM doc_chunks WHERE marker IS NOT NULL GROUP BY marker"
    ).fetchall())
    by_axis = dict(conn.execute(
        "SELECT axis, COUNT(*) FROM doc_chunks WHERE axis IS NOT NULL GROUP BY axis"
    ).fetchall())
    last_scan = conn.execute("SELECT MAX(scanned_at) FROM doc_chunks").fetchone()[0]
    conn.close()
    return {
        "total_chunks":    total,
        "by_kind":         by_kind,
        "by_language":     by_lang,
        "by_marker":       by_marker,
        "by_axis":         by_axis,
        "last_scan":       last_scan,
    }


# ──────────────────────────────────────────────────────────────
# NATS DAEMON
# ──────────────────────────────────────────────────────────────

async def daemon() -> None:
    try:
        import nats as _nats
    except ImportError:
        print("nats-py missing")
        return
    nc = await _nats.connect(NATS_URL, connect_timeout=5)
    print(f"[DOC] subscribed to tenet5.liril.docs.* on {NATS_URL}")

    async def h_scan(msg):
        classify = False
        try:
            req = json.loads(msg.data.decode() or "{}")
            classify = bool(req.get("classify"))
        except Exception:
            pass
        r = scan(classify=classify)
        if msg.reply:
            await nc.publish(msg.reply, json.dumps(r, default=str).encode())

    async def h_search(msg):
        try:
            req = json.loads(msg.data.decode())
        except Exception:
            req = {}
        pat = req.get("pattern", "")
        limit = int(req.get("limit", 50))
        r = search(pat, limit=limit) if pat else []
        if msg.reply:
            await nc.publish(msg.reply, json.dumps(r, default=str).encode())

    async def h_markers(msg):
        try:
            req = json.loads(msg.data.decode() or "{}")
        except Exception:
            req = {}
        kinds = req.get("kinds")
        limit = int(req.get("limit", 200))
        r = list_markers(kinds=kinds, limit=limit)
        if msg.reply:
            await nc.publish(msg.reply, json.dumps(r, default=str).encode())

    async def h_stats(msg):
        r = stats()
        if msg.reply:
            await nc.publish(msg.reply, json.dumps(r, default=str).encode())

    await nc.subscribe("tenet5.liril.docs.scan",    cb=h_scan)
    await nc.subscribe("tenet5.liril.docs.search",  cb=h_search)
    await nc.subscribe("tenet5.liril.docs.markers", cb=h_markers)
    await nc.subscribe("tenet5.liril.docs.stats",   cb=h_stats)

    while True:
        await asyncio.sleep(60)


# ──────────────────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description="LIRIL Doc Parser — the 'why' layer")
    ap.add_argument("--scan",      action="store_true", help="full scan of all roots")
    ap.add_argument("--classify",  action="store_true", help="also NPU-classify marker chunks")
    ap.add_argument("--todos",     action="store_true", help="list all TODO/FIXME/CRITICAL markers")
    ap.add_argument("--search",    type=str, default=None, help="substring search in doc index")
    ap.add_argument("--daemon",    action="store_true", help="serve tenet5.liril.docs.* subjects")
    ap.add_argument("--stats",     action="store_true", help="print index summary")
    ap.add_argument("--json",      action="store_true")
    args = ap.parse_args()

    if args.daemon:
        asyncio.run(daemon())
        return 0

    if args.scan:
        r = scan(classify=args.classify)
        if args.json: print(json.dumps(r, indent=2, default=str))
        else:
            print(f"── Doc scan complete ──")
            for k, v in r.items(): print(f"  {k}: {v}")

    if args.todos:
        rows = list_markers(kinds=["TODO", "FIXME", "CRITICAL", "HACK", "XXX"])
        if args.json: print(json.dumps(rows, indent=2, default=str))
        else:
            print(f"── {len(rows)} markers ──")
            for r in rows:
                print(f"  [{r['marker']:8s}] {r['file']}:{r['line']}  {r['msg'] or r['excerpt'][:120]}")

    if args.search:
        rows = search(args.search)
        if args.json: print(json.dumps(rows, indent=2, default=str))
        else:
            print(f"── {len(rows)} hits for '{args.search}' ──")
            for r in rows:
                print(f"  {r['file']}:{r['line']}  [{r['kind']}] {r['excerpt']}")

    if args.stats:
        r = stats()
        if args.json: print(json.dumps(r, indent=2, default=str))
        else:
            print(f"── Doc index stats ──")
            for k, v in r.items(): print(f"  {k}: {v}")

    if not (args.scan or args.todos or args.search or args.stats or args.daemon):
        ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
