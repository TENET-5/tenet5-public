#!/usr/bin/env python3
# Copyright (c) 2024-2026 Daniel Perry. All Rights Reserved.
# Licensed under EOSL-2.0.
# Modified: 2026-04-18T20:45:00Z | Author: claude_code | Change: LIRIL minimal memory store
"""LIRIL Memory — lightweight task similarity index (LIRIL's #D request).

Per LIRIL's own advancement directive via mercury.infer.code: build a
memory so the engineer can retrieve top-k most-similar past tasks at
inference time instead of relying only on same-axis-domain keyword
matching.

MINIMAL IMPLEMENTATION (no external dependencies):
  * sqlite3 (stdlib) for persistent index
  * Jaccard token similarity (stdlib) — no embeddings, no downloaded
    models, no vector-DB service
  * Reads past dev-team cycles from
    E:/TENET-5.github.io/data/liril_dev_team_log/*.json
  * Indexes (task_id, role, title, acceptance, text, schema_ok)
  * Exposes retrieve(role, query, k=3) returning the k most-similar
    PASS entries for that role

Future upgrade path: swap Jaccard for a real embedding (e.g. Mistral's
own internal embeddings via mercury subject, OR local sentence-
transformers if we accept the ~80MB download) without changing the
retrieve() interface.

CLI:
  python tools/liril_memory.py --rebuild       # rescan transcripts
  python tools/liril_memory.py --query "add reader_guidance json" --role engineer
  python tools/liril_memory.py --stats
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(r"E:/S.L.A.T.E/tenet5")
TRANSCRIPT_DIR = Path(r"E:/TENET-5.github.io/data/liril_dev_team_log")
DB_PATH = ROOT / "data" / "liril_memory.sqlite"


def _tokenize(s: str) -> set[str]:
    """Extract tokens (lowercase, alphanumeric, length >= 3)."""
    if not s:
        return set()
    return set(
        t for t in re.findall(r"[A-Za-z][A-Za-z0-9_]{2,}", s.lower())
        if len(t) >= 3 and t not in _STOPWORDS
    )


_STOPWORDS = {
    "the", "and", "for", "with", "from", "into", "this", "that", "they",
    "are", "was", "were", "will", "have", "has", "had", "should", "could",
    "would", "must", "need", "one", "two", "three", "all", "any", "you",
    "your", "not", "can", "per", "via", "use", "using", "add", "adds",
    "added", "new", "old", "real", "also", "just", "only", "our", "its",
    "more", "most", "some", "each", "then", "than", "when", "where",
    "which", "what", "why", "how", "who", "been", "but", "out", "off",
    "over", "under", "about", "between", "same", "other", "own", "every",
    "much", "many", "such", "these", "those",
}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


# ── Embedding generator (LIRIL self-request #1 — 2026-04-18 evening) ──
# Progressive enhancement: use real embeddings if any backend is available,
# else fall back to Jaccard. Tried in priority order:
#   1. sentence-transformers (all-MiniLM-L6-v2) — if installed
#   2. llama-server /v1/embeddings — if any 8082/8083 instance has --embeddings
#   3. OpenVINO genai (NPU path) — if an OV embedding model exists locally
#   4. None — Jaccard fallback
# On first use, each backend is probed once and the result cached.

_EMBED_BACKEND = None          # "st" / "llama" / "ov" / None
_EMBED_DIM: int | None = None
_ST_MODEL = None                 # lazy — sentence-transformers instance


def _try_import_st():
    """Try to load sentence-transformers all-MiniLM-L6-v2 once."""
    global _ST_MODEL
    if _ST_MODEL is not None:
        return _ST_MODEL
    try:
        from sentence_transformers import SentenceTransformer  # type: ignore
        _ST_MODEL = SentenceTransformer("all-MiniLM-L6-v2")
        return _ST_MODEL
    except Exception:
        return None


def _probe_llama_embeddings(url: str = "http://127.0.0.1:8082") -> bool:
    try:
        import urllib.request, urllib.error
        req = urllib.request.Request(
            f"{url}/v1/embeddings",
            data=__import__("json").dumps({"input": "probe", "model": "default"}).encode(),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            return 200 <= resp.status < 300
    except Exception:
        return False


def _select_backend() -> str | None:
    """Probe once; cache result."""
    global _EMBED_BACKEND
    if _EMBED_BACKEND is not None:
        return _EMBED_BACKEND or None
    if _try_import_st() is not None:
        _EMBED_BACKEND = "st"
        return "st"
    if _probe_llama_embeddings("http://127.0.0.1:8082"):
        _EMBED_BACKEND = "llama:8082"
        return "llama:8082"
    if _probe_llama_embeddings("http://127.0.0.1:8083"):
        _EMBED_BACKEND = "llama:8083"
        return "llama:8083"
    # OpenVINO path would go here — needs a local OV-format embedding model
    # which we don't ship. Skip for now, scaffolding is here for future.
    _EMBED_BACKEND = ""  # empty string = probed, none available
    return None


def embed(text: str) -> list[float] | None:
    """Return a vector embedding for text, or None if no backend available."""
    if not text or not text.strip():
        return None
    backend = _select_backend()
    if backend is None:
        return None
    try:
        if backend == "st":
            vec = _try_import_st().encode(text, normalize_embeddings=True).tolist()
            return vec
        if backend.startswith("llama:"):
            port = backend.split(":")[1]
            import urllib.request, json as _json
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/v1/embeddings",
                data=_json.dumps({"input": text[:8000], "model": "default"}).encode(),
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = _json.loads(resp.read())
                v = data.get("data", [{}])[0].get("embedding")
                if v:
                    # L2-normalize
                    import math as _math
                    norm = _math.sqrt(sum(x * x for x in v)) or 1.0
                    return [x / norm for x in v]
    except Exception:
        return None
    return None


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    # Both already normalized to unit length → cosine = dot product
    return sum(x * y for x, y in zip(a, b))


# ── DB schema ─────────────────────────────────────────────────────────

def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.executescript("""
      CREATE TABLE IF NOT EXISTS entries (
        cycle_id     TEXT NOT NULL,
        task_id      TEXT,
        role         TEXT NOT NULL,
        title        TEXT,
        axis_domain  TEXT,
        target_files TEXT,
        acceptance   TEXT,
        role_text    TEXT NOT NULL,
        schema_ok    INTEGER,
        tokens       TEXT,
        embedding    BLOB,
        mtime        REAL,
        PRIMARY KEY  (cycle_id, role)
      );
      CREATE INDEX IF NOT EXISTS idx_entries_role ON entries(role);
      CREATE INDEX IF NOT EXISTS idx_entries_axis ON entries(axis_domain);
    """)
    # Additive migration — add embedding column if not present
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(entries)").fetchall()]
        if "embedding" not in cols:
            conn.execute("ALTER TABLE entries ADD COLUMN embedding BLOB")
    except Exception:
        pass
    return conn


def _pack_embedding(vec: list[float] | None) -> bytes | None:
    if not vec:
        return None
    import struct
    return struct.pack(f"{len(vec)}f", *vec)


def _unpack_embedding(blob: bytes | None) -> list[float] | None:
    if not blob:
        return None
    import struct
    n = len(blob) // 4
    return list(struct.unpack(f"{n}f", blob))


def rebuild_from_transcripts(verbose: bool = False) -> dict:
    """Scan every transcript file in TRANSCRIPT_DIR and upsert entries."""
    if not TRANSCRIPT_DIR.exists():
        return {"scanned": 0, "inserted": 0, "note": f"no dir at {TRANSCRIPT_DIR}"}
    conn = _connect()
    try:
        inserted = 0
        scanned = 0
        for fp in sorted(TRANSCRIPT_DIR.glob("*.json")):
            scanned += 1
            try:
                entry = json.loads(fp.read_text(encoding="utf-8"))
            except Exception:
                continue
            task = entry.get("task", {}) or {}
            cycle_id = fp.stem  # e.g. WS-034_1776556181
            task_id = task.get("id", "?")
            axis = (task.get("axis_domain") or "").upper()
            title = task.get("title", "") or ""
            acceptance = task.get("acceptance", "") or ""
            target_files = json.dumps(task.get("target_files", []) or [])
            for rout in entry.get("roles", []) or []:
                role = rout.get("role")
                rtext = rout.get("text") or ""
                if not role or not rtext:
                    continue
                sok = 1 if rout.get("schema_ok") else 0
                tokens = _tokenize(title + " " + acceptance + " " + rtext)
                tokens_s = " ".join(sorted(tokens))
                # LIRIL self-request #1 (2026-04-18 evening): compute
                # embedding if backend available. No-op otherwise.
                emb_bytes: bytes | None = None
                try:
                    vec = embed((title + " " + acceptance + " " + rtext[:1000]).strip())
                    emb_bytes = _pack_embedding(vec)
                except Exception:
                    emb_bytes = None
                conn.execute(
                    "INSERT OR REPLACE INTO entries "
                    "(cycle_id, task_id, role, title, axis_domain, target_files, "
                    " acceptance, role_text, schema_ok, tokens, embedding, mtime) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (cycle_id, task_id, role, title, axis, target_files,
                     acceptance, rtext, sok, tokens_s, emb_bytes,
                     fp.stat().st_mtime),
                )
                inserted += 1
        conn.commit()
        if verbose:
            total = conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
            by_role = dict(conn.execute(
                "SELECT role, COUNT(*) FROM entries GROUP BY role"
            ).fetchall())
            print(f"scanned {scanned} transcripts, "
                  f"{inserted} role-entries upserted, {total} total in db")
            print(f"by role: {by_role}")
        return {"scanned": scanned, "inserted": inserted}
    finally:
        conn.close()


# ── Public retrieval API ─────────────────────────────────────────────

def retrieve(role: str, query: str, axis_domain: str | None = None,
             k: int = 3, only_schema_ok: bool = True) -> list[dict]:
    """Return up to k past role entries ranked by similarity to query.

    Uses real embedding cosine similarity if any embedding backend is
    available and both the query + entries have embeddings; otherwise
    falls back to Jaccard on tokens.
    """
    conn = _connect()
    try:
        q_tokens = _tokenize(query)
        if not q_tokens:
            return []
        # Compute query embedding once (may be None if no backend).
        q_vec = embed(query)
        sql = ("SELECT cycle_id, task_id, role, title, axis_domain, target_files, "
               "acceptance, role_text, schema_ok, tokens, mtime, embedding "
               "FROM entries WHERE role = ?")
        params: list = [role]
        if only_schema_ok:
            sql += " AND schema_ok = 1"
        if axis_domain:
            sql += " AND axis_domain = ?"
            params.append(axis_domain.upper())
        rows = conn.execute(sql, tuple(params)).fetchall()
        scored = []
        used_embeddings = False
        for r in rows:
            emb = _unpack_embedding(r[11])
            if q_vec is not None and emb is not None:
                sim = _cosine(q_vec, emb)
                used_embeddings = True
            else:
                toks = set((r[9] or "").split())
                sim = _jaccard(q_tokens, toks)
            if sim > 0:
                scored.append((sim, r))
        scored.sort(key=lambda x: (-x[0], -x[1][10]))  # sim desc, then mtime desc
        out = []
        for sim, r in scored[:k]:
            out.append({
                "cycle_id":   r[0], "task_id": r[1], "role": r[2],
                "title":      r[3], "axis_domain": r[4],
                "target_files": json.loads(r[5]) if r[5] else [],
                "acceptance": r[6], "role_text": r[7],
                "schema_ok":  bool(r[8]), "similarity": round(sim, 4),
                "mtime":      r[10],
                "score_method": "cosine" if used_embeddings else "jaccard",
            })
        return out
    finally:
        conn.close()


def stats() -> dict:
    """Return summary statistics about the memory."""
    conn = _connect()
    try:
        total = conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
        ok = conn.execute("SELECT COUNT(*) FROM entries WHERE schema_ok = 1").fetchone()[0]
        by_role = dict(conn.execute(
            "SELECT role, COUNT(*) FROM entries GROUP BY role"
        ).fetchall())
        by_axis = dict(conn.execute(
            "SELECT axis_domain, COUNT(*) FROM entries GROUP BY axis_domain"
        ).fetchall())
        return {
            "total_entries": total,
            "schema_ok_entries": ok,
            "by_role": by_role,
            "by_axis": by_axis,
        }
    finally:
        conn.close()


def main():
    p = argparse.ArgumentParser(description="LIRIL lightweight task memory")
    p.add_argument("--rebuild", action="store_true", help="rescan transcripts")
    p.add_argument("--stats", action="store_true", help="show db stats")
    p.add_argument("--query", type=str, help="similarity query")
    p.add_argument("--role", type=str, default="engineer", help="which role")
    p.add_argument("--axis", type=str, help="filter axis_domain (e.g. ETHICS)")
    p.add_argument("-k", type=int, default=3, help="top-k results")
    args = p.parse_args()

    if args.rebuild:
        r = rebuild_from_transcripts(verbose=True)
        print(json.dumps(r, indent=2))
        return
    if args.stats:
        print(json.dumps(stats(), indent=2))
        return
    if args.query:
        hits = retrieve(args.role, args.query, axis_domain=args.axis, k=args.k)
        print(f"top {len(hits)} similar {args.role!r} entries:")
        for h in hits:
            preview = h["role_text"][:120].replace("\n", " ")
            print(f"  sim={h['similarity']:.3f}  {h['cycle_id']:28s}  "
                  f"{h['axis_domain']:12s} {h['title'][:60]}")
            print(f"    → {preview}…")
        return
    p.print_help()


if __name__ == "__main__":
    main()
