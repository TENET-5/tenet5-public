# Copyright (c) 2024-2026 Daniel Perry. All Rights Reserved.
# Licensed under EOSL-2.0.
# Modified: 2026-04-18T04:45:00Z
"""LIRIL Autonomous Dev Team — operates LIRIL as a multi-role dev team
with a visible work schedule on the website.

User directive (2026-04-18 00:11):
  "liril should be automatically writing and maintaining a work schedule
   as a full dev team on the website"

This is the focused replacement for the prior garbage-emitting autonomous
stack. Key differences:
  - Reads the backlog from data/liril_work_schedule.json (public board).
  - Each task is routed through a pipeline of named roles
    (researcher, architect, designer, engineer, editor, gatekeeper).
  - Every role's output is a structured JSON answer from LIRIL infer.
  - The gatekeeper role is the hallucination gate: it demands
    VERDICT: PASS|WATCH|FAIL. Only PASS commits.
  - Every commit message is plain English, subject under 72 chars.
  - Runs on the working NATS 4223 bus (not the broken legacy).
  - Writes itself to the board (in_progress, done_today) so the user
    can SEE what LIRIL is doing in real time.

Usage:
  # one cycle (picks one task, executes, commits if PASS)
  python tools/liril_dev_team.py --once

  # continuous 20-minute cycle
  python tools/liril_dev_team.py --daemon

  # dry-run — execute task without writing or committing
  python tools/liril_dev_team.py --once --dry-run
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as _dt
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
import random
import subprocess
import sys
import time
from pathlib import Path

# Windows: suppress console popup on every subprocess call made by this
# daemon. Without this, every cycle's git add/diff/commit/checkout
# flashes a conhost.exe on the taskbar for ~100ms and the user sees a
# popup storm every ~2 minutes.
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

# mercury.infer (llama-server bridge via NemoServer) lives on port 4222.
# tenet5.liril.* (NPU orchestrator) lives on port 4223. The dev-team roles
# are all inference prompts, so we connect to 4222 where mercury.infer
# has a responder. If we need classify/advise in a later cycle, open a
# second connection to 4223.
# 2026-04-18: use plain assignment (not setdefault) so stale shell env
# vars pointing at 14222 (Docker guest — no mercury.infer responder)
# don't silently route every dev-team cycle to a dead subject. This
# bug caused every cycle to return UNPARSED for weeks until the GPU
# came back AND this override was added.
os.environ["NATS_URL"]      = "nats://127.0.0.1:4223"
os.environ["NATS_URL_HOST"] = "nats://127.0.0.1:4223"

import nats  # type: ignore

# Role-spec validators — enforce per-role output schema between pipeline
# stages. Malformed output gets one retry pass with stricter instructions,
# then the role is marked failed.
try:
    from liril_role_spec import validate_role_output
except ImportError:
    # Importable from the tools/ directory directly.
    import sys
    sys.path.insert(0, str(Path(__file__).parent))
    from liril_role_spec import validate_role_output

SITE = Path(r"E:\TENET-5.github.io")
BOARD = SITE / "data" / "liril_work_schedule.json"
LOG_DIR = SITE / "data" / "liril_dev_team_log"
LOG_DIR.mkdir(parents=True, exist_ok=True)

BANNED_SUBJECT_PATTERNS = [
    "[PHASE ", "[LIRIL PHASE ", "ABCXYZ", "Millennial Falcon",
    "Empirical Magic Handoff", "Atmosphere optimizer", "Genocide-Evidence EMH",
    "Zero-Orphan disk fallback", "ActionGuard-verified", "N-vs-NP convergence",
    "EMH tracker badge", "Mass inject",
]


def _now_iso() -> str:
    return _dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def _log(tag: str, msg: str) -> None:
    stamp = _dt.datetime.now().strftime("%H:%M:%S")
    print(f"  [{stamp}] [{tag}] {msg}", flush=True)


def load_board() -> dict:
    return json.loads(BOARD.read_text(encoding="utf-8"))


def save_board(board: dict) -> None:
    board["updated_at"] = _now_iso()
    BOARD.write_text(json.dumps(board, ensure_ascii=False, indent=2), encoding="utf-8")


ROLE_PROMPTS = {
    "researcher": (
        "You are the RESEARCHER on a TENET5 dev team. Your job: identify exactly "
        "one specific primary-source fact needed to complete the task. You must "
        "name the source document (e.g. 'AG 2024 Report 1 para 1.47' or "
        "'Hansard 44-1 #123 col 5320') and quote it in one sentence. "
        "If no external primary source is needed (internal schema/reader-guidance "
        "changes), emit SOURCE: 'internal TENET5 change' and QUOTE: '(internal)'. "
        "Do NOT invent paragraph IDs or report numbers.\n\n"
        "{few_shot}"
        "Task:\n{task}\n\nRespond in this exact format:\n"
        "SOURCE: <short citation>\n"
        "QUOTE: <one sentence from the source>\n"
        "GAP_FILLED: <one sentence saying what this lets us add>\n"
    ),
    "architect": (
        "You are the ARCHITECT on a TENET5 dev team. Your job: name the exact "
        "filename and the exact insertion point (by heading or line-landmark) "
        "where the research finding from the previous role should land.\n\n"
        "Task:\n{task}\n\nPrior-role output:\n{prior}\n\n"
        "Respond in this exact format:\n"
        "FILE: <path>\n"
        "ANCHOR: <heading text or existing line to insert near>\n"
        "POSITION: <before|after|replace>\n"
    ),
    "designer": (
        "You are the DESIGNER on a TENET5 dev team. Your job: propose the "
        "visual/UX form the change should take. Follow Shneiderman "
        "overview-first, NN/g readability, WCAG 2.1 AA. Be brief.\n\n"
        "Task:\n{task}\n\nPrior-role output:\n{prior}\n\n"
        "Respond in this exact format:\n"
        "FORM: <card | inline paragraph | list item | badge>\n"
        "ACCESSIBILITY: <one sentence: ARIA/alt/contrast>\n"
        "COPY_TONE: <concise | investigative | procedural>\n"
    ),
    "engineer": (
        # 2026-04-19: rewritten after runtime profiler flagged engineer
        # role at 67.6% schema-ok vs architect 96.9%. LIRIL's own
        # diagnosis: 'lack of standardization in engineering processes'.
        # New structure: hard-coded worked example FIRST, format
        # checklist BEFORE task text, explicit failure modes listed.
        # Gives the model a concrete target shape instead of inferring
        # from prose.
        "You are the ENGINEER on a TENET5 dev team.\n\n"
        "═══ OUTPUT CONTRACT — FOLLOW EXACTLY ═══\n"
        "1. Emit ONE fenced code block with the correct language tag.\n"
        "2. Fence MUST open with ``` followed by a language (json|html|js|md).\n"
        "3. Fence MUST close with ```.\n"
        "4. ZERO text before the opening fence.\n"
        "5. ZERO text after the closing fence.\n"
        "6. For JSON targets: the body MUST be a COMPLETE object wrapping\n"
        "   your new key(s), not a fragment. The applier MERGES new top-\n"
        "   level keys into the existing file.\n"
        "7. Keep body ≤ ~5000 chars (budget 1536 tokens). Longer outputs\n"
        "   are truncated by the model before the closing fence.\n"
        "8. Reuse URLs that already appear in the target file. Do NOT\n"
        "   invent new URLs — the URL hallucination gate rejects dead\n"
        "   URLs and halts the cycle.\n\n"
        "═══ WORKED EXAMPLE (JSON target) ═══\n"
        "Task: Add top-level 'reader_guidance' dict to data/my_file.json\n\n"
        "CORRECT output:\n"
        "```json\n"
        "{{\n"
        "  \"reader_guidance\": {{\n"
        "    \"how_to_read\": \"Each row is one event with a primary-source URL.\",\n"
        "    \"what_to_verify\": \"Click through to the source and confirm the date.\"\n"
        "  }}\n"
        "}}\n"
        "```\n\n"
        "WRONG — fragment (applier rejects):\n"
        "```json\n"
        "\"reader_guidance\": {{ ... }}\n"
        "```\n\n"
        "WRONG — prose wrapper around code (validator rejects):\n"
        "Here is the JSON: ```json {{ ... }} ```\n\n"
        "═══ WORKED EXAMPLE (HTML target) ═══\n"
        "Task: Add a 'Read next' sidebar to about.html before the footer\n\n"
        "CORRECT output:\n"
        "```html\n"
        "<aside class=\"read-next\">\n"
        "  <h3>Read next</h3>\n"
        "  <ul>\n"
        "    <li><a href=\"/my-story.html\">The record</a></li>\n"
        "  </ul>\n"
        "</aside>\n"
        "```\n\n"
        "═══ CONTEXT ═══\n"
        "{target_file_context}"
        "{few_shot}"
        "Task:\n{task}\n\n"
        "Prior-role output:\n{prior}\n\n"
        "═══ EMIT NOW ═══\n"
        "Return ONLY the fenced code block. No preamble, no explanation."
    ),
    "editor": (
        "You are the EDITOR on a TENET5 dev team. Your job: copy-check the "
        "engineer's output. Flag any claim without a source, any broken link, "
        "any accessibility miss. If clean, say so.\n\n"
        "Task:\n{task}\n\nEngineer output:\n{prior}\n\n"
        "Respond in this exact format:\n"
        "CHECK_CITATIONS: <OK | missing: ...>\n"
        "CHECK_LINKS: <OK | broken: ...>\n"
        "CHECK_A11Y: <OK | missing: ...>\n"
        "OVERALL: <CLEAN | NEEDS_REWORK>\n"
    ),
    "gatekeeper": (
        "/no_think\n\nYou are the GATEKEEPER on a TENET5 dev team. Your job: "
        "the hallucination gate. You emit exactly two lines. Do not reason "
        "aloud.\n\n"
        "{few_shot}"
        "Task:\n{task}\n\n"
        "Pipeline output so far:\n{prior}\n\n"
        "DELIBERATION CHECKLIST (run silently, then emit the two lines):\n"
        "  1. Did the task's acceptance criterion imply a file change "
        "     (words like 'add', 'write', 'update', 'create')? If yes, did "
        "     the engineer role emit a fenced code block with the literal "
        "     change? If the engineer role is absent from the pipeline or "
        "     the engineer's output was empty, commentary, or prose — emit "
        "     FAIL with reason 'pipeline produced no concrete deliverable'.\n"
        "  2. Does the proposed change cite a specific primary source "
        "     (Canada Gazette OIC number, CanLII case cite, Hansard "
        "     reference, PBO report ID, AG report number, Commissioner "
        "     report URL)? If a citation is missing or invented, emit FAIL.\n"
        "  3. Does the engineer output overwrite any existing fact? If it "
        "     replaces a verified claim with a different one, emit FAIL.\n"
        "  4. Is there a date, name, or section number that looks slightly "
        "     off but you cannot be sure? Emit WATCH.\n"
        "  5. Otherwise, emit PASS.\n\n"
        "Emit ONLY these two lines, exactly:\n"
        "VERDICT: PASS\n"
        "REASON: <one short sentence>\n\n"
        "Verdict choices: PASS, WATCH, FAIL. Start your response with "
        "'VERDICT:' and stop after REASON.\n"
        "VERDICT:"
    ),
}


async def call_liril_infer(nc, prompt: str, max_tokens: int = 320, timeout: int = 60) -> tuple[str, int]:
    # Route through mercury.infer.code — this subject skips TIER 1 (LOOM
    # symbolic compiler) which was intercepting dev-team prompts and
    # returning a cached "LOOM has 41 distinct Fraction-exact ratios"
    # response for every role. Code subject routes directly to TIER 3
    # (llama-server on GPU).
    #
    # 2026-04-18 LIRIL #5 follow-up: we MUST send `messages` not `prompt`.
    # nemo_server._handle_code_infer has a default fallback that wraps
    # req["prompt"] inside a user message templated as:
    #   system: "You are an expert {language} developer. ... No explanations, just code."
    #   user:   "Task: {prompt}\nFile: {file_path}\n\nWrite the code:"
    # That wrapper contradicts TENET5 roles (engineer writes JSON/HTML not
    # "code"; editor evaluates outputs; gatekeeper emits two lines). The
    # contradiction produces empty outputs and wrong-format outputs —
    # which drove the 37.5% engineer_empty rate on recent-40. Sending
    # messages directly bypasses the default wrapper.
    # /no_think prefix was previously added to the prompt; now baked
    # into the system message so it still applies.
    messages = [
        {"role": "system",
         "content": "/no_think\n\nYou are a TENET5 dev-team role. Follow the"
                    " user instruction exactly. Do not add explanation, do not"
                    " add preamble, emit only the format the instruction"
                    " specifies."},
        {"role": "user", "content": prompt},
    ]
    payload = json.dumps({
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0.2,
        # 2026-04-18: disable Nemotron-3-Nano reasoning tokens.
        # The 30B MoE wraps output in <think>...</think> by default,
        # which the OpenAI-format server strips, producing empty content.
        # Setting enable_thinking=False makes the model emit directly.
        # Ignored by non-reasoning models (Mistral-Nemo, Llama-3.x).
        "chat_template_kwargs": {"enable_thinking": False},
    }).encode("utf-8")
    t0 = time.time()
    # 2026-04-18 LIRIL #5 follow-up: retry on transient NATS errors.
    # error_during_role was 11.5% on recent-200 (#2 failure category)
    # because NoRespondersError bubbled up to the pipeline and killed
    # the cycle. Nemo_server occasionally restarts / misses a subject
    # for a few hundred ms; a simple 2-attempt retry with short backoff
    # eliminates that failure mode without masking genuine outages.
    last_err: Exception | None = None
    for attempt in range(2):
        try:
            msg = await nc.request("mercury.infer.code", payload, timeout=timeout)
            last_err = None
            break
        except Exception as e:
            last_err = e
            cls = type(e).__name__
            # Only retry on transient NATS no-responders / timeout. Don't
            # retry on programmer errors (ValueError, TypeError, etc.).
            if cls not in ("NoRespondersError", "TimeoutError",
                           "ConnectionClosedError", "OutboundQueueFull"):
                raise
            if attempt == 0:
                _log("INFER-RETRY", f"{cls} on attempt 1 — retrying after 1.5s")
                await asyncio.sleep(1.5)
    if last_err is not None:
        raise last_err
    dt_ms = int((time.time() - t0) * 1000)
    envelope = json.loads(msg.data.decode("utf-8", errors="replace"))
    text = envelope.get("text", "")
    return text, dt_ms


def _load_few_shot_examples(role: str, task: dict, k: int = 2) -> str:
    """In-context learning via LIRIL memory (stdlib sqlite + Jaccard).

    2026-04-18 v1: file-scan per run, mtime-ranked. Cheap but loses
    keyword relevance.
    2026-04-18 v2 (THIS): delegate to tools/liril_memory.py which
    holds a persistent sqlite index with Jaccard token similarity.
    Returns top-k most-similar past role outputs in the same
    axis_domain where schema_ok == True.
    """
    if role not in ("engineer", "researcher", "editor", "gatekeeper"):
        return ""
    try:
        import sys as _sys
        tools_dir = str(Path(__file__).parent)
        if tools_dir not in _sys.path:
            _sys.path.insert(0, tools_dir)
        import liril_memory as _mem
    except Exception:
        return ""
    # Query = title + acceptance + target files; this is what makes this
    # task semantically similar to past ones.
    query = " ".join(filter(None, [
        task.get("title", ""),
        task.get("acceptance", ""),
        " ".join(task.get("target_files", []) or []),
    ]))
    if not query.strip():
        return ""
    axis = (task.get("axis_domain") or "").upper() or None
    try:
        hits = _mem.retrieve(role, query, axis_domain=axis, k=k, only_schema_ok=True)
    except Exception:
        return ""
    if not hits:
        # Try without axis filter — cross-domain examples are still useful
        try:
            hits = _mem.retrieve(role, query, axis_domain=None, k=k, only_schema_ok=True)
        except Exception:
            return ""
    if not hits:
        return ""
    lines = [f"Here are the {len(hits)} most-similar past {role} outputs "
             "(same format, past cycles that PASSED):\n"]
    for i, h in enumerate(hits, 1):
        lines.append(f"  === example {i} (similarity={h['similarity']}) ===")
        lines.append(f"  Task: {h.get('title','?')}")
        body = (h.get("role_text") or "").strip()
        if len(body) > 600:
            body = body[:600] + "…"
        for bl in body.splitlines():
            lines.append("    " + bl)
        lines.append("  === end example ===\n")
    return "\n".join(lines) + "\n"


def _load_verified_url_bank(task: dict, max_urls: int = 12) -> str:
    """LIRIL option E (partial): extract http/https URLs from past
    schema_ok engineer transcripts in the same axis_domain, then
    include as a suggestions list. Complements the citation-rules in
    the prompt — gives the model concrete URLs it has seen pass the
    hallucination gate before.
    """
    try:
        import sys as _sys
        tools_dir = str(Path(__file__).parent)
        if tools_dir not in _sys.path:
            _sys.path.insert(0, tools_dir)
        import liril_memory as _mem
    except Exception:
        return ""
    axis = (task.get("axis_domain") or "").upper() or None
    query = " ".join(filter(None, [
        task.get("title", ""),
        task.get("acceptance", ""),
    ]))
    if not query.strip():
        return ""
    try:
        hits = _mem.retrieve("engineer", query, axis_domain=axis,
                             k=6, only_schema_ok=True)
    except Exception:
        return ""
    if not hits:
        return ""
    url_re = __import__("re").compile(r'https?://[^\s"\'<>)`\]\\]+')
    seen = set()
    urls = []
    for h in hits:
        for m in url_re.finditer(h.get("role_text") or ""):
            u = m.group(0).rstrip('.,;:!?)"\']>')
            if u not in seen:
                seen.add(u)
                urls.append(u)
            if len(urls) >= max_urls:
                break
        if len(urls) >= max_urls:
            break
    if not urls:
        return ""
    return ("KNOWN-GOOD CITATION URLs (have passed the hallucination gate "
            "in past cycles for this axis — prefer these over inventing new ones):\n"
            + "\n".join(f"  {u}" for u in urls) + "\n\n")


def _load_target_file_context(task: dict, max_chars: int = 2000) -> str:
    """LIRIL option C (PRIOR-FILE-INJECTION): when the engineer is
    about to modify a file, include the file's CURRENT content (first
    N chars) in the prompt so the output is structurally compatible.

    Addresses the failure mode observed in WS-033/034 where the
    engineer invented a JSON schema incompatible with the actual file
    structure and the diff-apply rejected it.

    Only includes JSON/HTML/MD/JS files that are < 200 KB (anything
    larger is truncated). Skips binary, image, or non-existent files.
    """
    target_files = task.get("target_files", []) or []
    if not target_files:
        return ""
    site = Path(r"E:/TENET-5.github.io")
    pieces = []
    for tf in target_files[:2]:  # cap at 2 files
        tf_path = site / tf
        if not tf_path.exists():
            continue
        try:
            sz = tf_path.stat().st_size
            if sz > 200_000:
                pieces.append(f"  [{tf} is {sz} bytes — too large to inline]\n")
                continue
            if sz == 0:
                pieces.append(f"  [{tf} exists but is empty — engineer should emit the whole file]\n")
                continue
            ext = tf.rsplit(".", 1)[-1].lower() if "." in tf else ""
            if ext not in ("json", "html", "md", "js", "css", "jsonl"):
                pieces.append(f"  [{tf} has unsupported extension .{ext}]\n")
                continue
            content = tf_path.read_text(encoding="utf-8", errors="replace")
            if len(content) > max_chars:
                content = content[:max_chars] + "\n  …[truncated]…"
            pieces.append(f"\nCurrent content of {tf}:\n```{ext}\n{content}\n```\n")
        except Exception as e:
            pieces.append(f"  [{tf} could not be read: {e!r}]\n")
    if not pieces:
        return ""
    return ("EXISTING TARGET FILE (engineer output must be structurally compatible):\n"
            + "".join(pieces) + "\n")


async def run_role(nc, role: str, task: dict, prior: str) -> dict:
    """Run a single role, validate its output, retry once with stricter
    instructions if the schema check fails. This is the between-role
    quality gate — catches malformed outputs before they cascade.
    """
    # LIRIL's self-requested few-shot learning (rolled out role-by-role).
    # 2026-04-18 first pass: engineer only.
    # 2026-04-18 second pass (LIRIL pick 'A'): extend to researcher and
    # gatekeeper — the two other roles with strict output schemas where
    # seeing a past valid example meaningfully helps format compliance.
    # Skipping designer (rarely run) and architect/editor (flexible).
    few_shot = _load_few_shot_examples(role, task, k=2) if role in ("engineer", "researcher", "gatekeeper") else ""
    # LIRIL option C (2026-04-18): engineer sees target-file current
    # content so output is structurally compatible. Reduces the UNPARSED
    # tail where novel JSON schemas don't match existing file structure.
    target_ctx = _load_target_file_context(task) if role == "engineer" else ""
    # 2026-04-18: URL bank + heavy citation-rules block REMOVED after
    # measured regression (50% -> 30% PASS rate on 10-cycle burn). The
    # additional ~900 chars of prompt context pushed the model into
    # short/empty outputs. Target-file injection ALONE was net positive
    # (WS-030 moved from UNPARSED to PASS); the URL bank / citation
    # rules on top of that were net negative.
    # Keeping only: target_file_context + one-sentence 'reuse existing
    # URLs' instruction in the main prompt. _load_verified_url_bank
    # kept as dead code for possible future re-enable with shorter copy.
    raw_prompt = ROLE_PROMPTS[role]
    fmt_args = {"task": json.dumps(task, ensure_ascii=False, indent=2),
                "prior": prior, "few_shot": few_shot,
                "target_file_context": target_ctx}
    # Only engineer prompt uses {few_shot} + {target_file_context}; others won't have the tokens
    try:
        prompt = raw_prompt.format(**fmt_args)
    except KeyError:
        # Back-compat: other role prompts without few_shot/target_file_context
        prompt = raw_prompt.format(task=fmt_args["task"], prior=fmt_args["prior"])
    # LIRIL #5 follow-up (2026-04-18): engineer_empty 37.5% in recent-40
    # was NOT prompt-bloat (trimming target_file_context 2000→1200 chars
    # made it worse). Root cause: default max_tokens=320 only buys ~1280
    # chars of output, but engineer prompt asks for up to 1500 chars of
    # JSON. Model reasons, hits limit, emits nothing. Role-specific
    # token budgets fix this: engineer needs much more, gatekeeper much
    # less (two lines only), editor medium (fixed 4-field format).
    role_max_tokens = {
        # 2026-04-18: engineer bumped 1024→1536 after WS-033 confirmed
        # truncation at ~1510 chars mid-JSON (no closing fence). 1536
        # tokens ≈ 5 KB output, enough for ~20-entry array tasks.
        # Validator also now tolerates half-closed fences as defence
        # in depth.
        "engineer":   1536,   # code diffs up to ~5 KB (large arrays, dicts)
        "researcher": 512,    # SOURCE/QUOTE/GAP_FILLED fields + headroom
        "architect":  384,    # short plan output
        "designer":   320,    # rare, keep default
        "editor":     256,    # fixed 4-line CHECK_* / OVERALL format
        "gatekeeper": 192,    # two lines + headroom (p99 seen = 197 tokens)
    }
    budget = role_max_tokens.get(role, 320)
    try:
        text, dt_ms = await call_liril_infer(nc, prompt, max_tokens=budget)
    except Exception as e:
        return {"role": role, "error": f"infer-failed: {e!r}", "text": "", "latency_ms": None}

    # First-pass validation
    ok, issues = validate_role_output(role, text)
    retried = False
    retry_issues = []
    if not ok:
        _log("VALIDATE", f"{role}: failed ({issues!r}) — one retry with stricter prompt")
        retried = True
        retry_prompt = (
            prompt
            + "\n\nYOUR PREVIOUS OUTPUT FAILED SCHEMA CHECK:\n"
            + "\n".join(f"  - {i}" for i in issues)
            + "\n\nRe-emit the output, strictly in the format specified. "
            + "No prose, no apology, just the required fields."
        )
        try:
            text2, dt2 = await call_liril_infer(nc, retry_prompt, max_tokens=budget)
            ok2, issues2 = validate_role_output(role, text2)
            retry_issues = issues2
            if ok2:
                text, dt_ms = text2, dt_ms + dt2
                ok = True
                issues = []
            else:
                # keep the retry text so the transcript has both attempts
                text = text2
                dt_ms += dt2
                issues = issues2
        except Exception as e:
            # retry failed to infer — return original + note
            pass

    out = {
        "role": role,
        "text": text,
        "latency_ms": dt_ms,
        "schema_ok": ok,
        "schema_issues": issues,
        "retried": retried,
    }
    # 2026-04-19: capture failing prompts so we can diagnose post-hoc why the
    # model returned empty / wrong-format. Only save when schema fails (keeps
    # transcripts small and avoids leaking large prompts unnecessarily).
    if not ok:
        out["prompt_len"] = len(prompt)
        # Cap saved prompt at 4000 chars — enough to reproduce while keeping
        # transcripts under ~10 KB each.
        out["prompt_sample"] = prompt if len(prompt) <= 4000 else (prompt[:2000] + "\n... [truncated] ...\n" + prompt[-1500:])
        out["budget"] = budget
    return out


# ══════════════════════════════════════════════════════════════════════════
# Hallucination gate — URL HEAD check for engineer output (LIRIL #3)
# Runs inside the pipeline between engineer and editor. Any http/https URL
# in the engineer's diff must return HTTP 2xx or 3xx on HEAD within 8s, or
# the task fails with status=hallucination_caught. Prevents the model from
# landing invented URLs like phac-aspc.gc.ca/im/vs-sv/... that don't exist.
# ══════════════════════════════════════════════════════════════════════════
import re as _re_urls
import urllib.request as _urllib_req
import urllib.error as _urllib_err
import concurrent.futures as _cf


def _extract_urls(text: str) -> list[str]:
    if not text:
        return []
    pat = _re_urls.compile(r'https?://[^\s"\'<>)`\]\\]+')
    seen = set()
    out = []
    for m in pat.finditer(text):
        url = m.group(0).rstrip('.,;:!?)"\']>')
        if url not in seen:
            seen.add(url)
            out.append(url)
    return out


def _head_check(url: str, timeout: float = 8.0) -> tuple[bool, str]:
    req = _urllib_req.Request(url, method="HEAD",
                              headers={"User-Agent": "TENET5-LIRIL-hallucination-gate/1.0"})
    try:
        with _urllib_req.urlopen(req, timeout=timeout) as resp:
            if 200 <= resp.status < 400:
                return True, f"HTTP {resp.status}"
            return False, f"HTTP {resp.status}"
    except _urllib_err.HTTPError as e:
        # Some servers 405 on HEAD but 200 on GET — accept 405/403 as
        # "URL exists, method just not allowed". 404/410 are real dead.
        if e.code in (403, 405, 429):
            return True, f"HTTP {e.code} (HEAD blocked — URL exists)"
        return False, f"HTTP {e.code}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def _check_urls_in_text(text: str) -> tuple[bool, list[str]]:
    urls = _extract_urls(text)
    if not urls:
        return True, []
    dead = []
    with _cf.ThreadPoolExecutor(max_workers=6) as ex:
        futures = {ex.submit(_head_check, u): u for u in urls}
        for fut in _cf.as_completed(futures, timeout=30):
            url = futures[fut]
            try:
                ok, detail = fut.result()
            except Exception as e:
                ok, detail = False, f"exception: {e!r}"
            if not ok:
                dead.append(f"{url} — {detail}")
    return (not dead), dead


async def execute_task(nc, task: dict) -> dict:
    role_outputs: list[dict] = []
    prior = ""
    for role in task.get("role_pipeline", []):
        _log("ROLE", f"→ {role} on {task['id']}")
        out = await run_role(nc, role, task, prior)
        role_outputs.append(out)
        if "error" in out:
            return {"task": task, "roles": role_outputs, "status": "error_during_role", "error_role": role}
        # Schema gate: if a role's output failed validation even after one
        # retry, halt the pipeline — don't let bad output cascade.
        if out.get("schema_ok") is False:
            return {
                "task": task,
                "roles": role_outputs,
                "status": "schema_validation_failed",
                "failed_role": role,
                "schema_issues": out.get("schema_issues", []),
            }
        # ── Hallucination gate (LIRIL self-requested #3) ──────────────
        # After engineer produces diff, verify every URL in the output
        # returns HTTP 2xx/3xx (or known-allowlisted 403/405). If any
        # URL 404s, the task fails before commit — no hallucinated
        # sources land.
        if role == "engineer":
            ok, dead_urls = _check_urls_in_text(out.get("text", "") or "")
            if not ok:
                _log("HALLUCINATION-GATE",
                     f"{len(dead_urls)} dead URL(s) in engineer output — halting")
                for du in dead_urls[:5]:
                    _log("HALLUCINATION-GATE", f"  {du}")
                return {
                    "task": task,
                    "roles": role_outputs,
                    "status": "hallucination_caught",
                    "failed_role": role,
                    "dead_urls": dead_urls,
                }
            elif dead_urls == []:
                urls_seen = len(_extract_urls(out.get("text", "") or ""))
                if urls_seen:
                    _log("HALLUCINATION-GATE", f"{urls_seen} URL(s) verified live")
        prior = out["text"]
        if role == "editor" and "NEEDS_REWORK" in (out.get("text", "") or ""):
            return {"task": task, "roles": role_outputs, "status": "editor_requested_rework"}

    # ── Verdict parser (LIRIL self-requested 2026-04-18) ──────────────
    # Previous version ONLY looked for a line starting with "VERDICT:" —
    # if the gatekeeper surrounded the verdict with markdown, emoji, or
    # expanded it into prose ("I assess this output as PASS"), the parser
    # fell back to UNPARSED even when the signal was obvious. That's why
    # 40-50% of cycles returned UNPARSED in aggregate.
    #
    # Now: (a) keep the strict "VERDICT:" line parser as primary; then
    # (b) scan the whole gatekeeper output for the first occurrence of
    # PASS/WATCH/FAIL as a standalone word; then (c) if neither found,
    # fall back to the editor's OVERALL assessment if the engineer
    # produced valid JSON (we trust the schema gate when the narrator
    # role fails its own formatting).
    gk = next((r for r in role_outputs if r["role"] == "gatekeeper"), None)
    ed = next((r for r in role_outputs if r["role"] == "editor"), None)
    eng = next((r for r in role_outputs if r["role"] == "engineer"), None)
    verdict = "UNPARSED"
    reason = ""

    if gk:
        gk_text = gk.get("text", "") or ""
        # (a) Strict primary parse
        for line in gk_text.splitlines():
            L = line.strip()
            if L.upper().startswith("VERDICT:"):
                rest = L.split(":", 1)[1].strip()
                if rest:
                    first = rest.split()[0].upper().strip(".,;:*`_\"'()[]")
                    if first in ("PASS", "WATCH", "FAIL"):
                        verdict = first
            elif L.upper().startswith("REASON:"):
                reason = L.split(":", 1)[1].strip()[:200]
        # (b) Standalone-word scan fallback
        if verdict == "UNPARSED":
            import re as _re
            for token in ("PASS", "WATCH", "FAIL"):
                if _re.search(r"\b" + token + r"\b", gk_text.upper()):
                    verdict = token
                    if not reason:
                        # Best-effort reason extraction from first non-empty line
                        for L in gk_text.splitlines():
                            if L.strip() and not L.strip().upper().startswith("VERDICT"):
                                reason = L.strip()[:200]
                                break
                    break

    # (c) Editor-overall fallback when gatekeeper still ambiguous.
    # Only trust this when engineer schema passed — otherwise the diff
    # shouldn't land.
    if verdict == "UNPARSED" and ed and eng:
        ed_text = (ed.get("text", "") or "").upper()
        eng_ok = eng.get("schema_ok") is True
        if eng_ok and "OVERALL:" in ed_text:
            for line in ed_text.splitlines():
                if line.strip().startswith("OVERALL:"):
                    rest = line.split(":", 1)[1].strip().split()[0].strip(".,;:*`")
                    if rest in ("PASS",):
                        verdict = "PASS"
                        reason = ("gatekeeper-output-unparseable; fallback to editor "
                                  "OVERALL: PASS with engineer schema-ok")
                        _log("PARSER", "fallback verdict via editor-overall: PASS")
                    elif rest in ("WATCH", "FAIL"):
                        verdict = rest
                        reason = f"gatekeeper unparseable; fallback to editor OVERALL: {rest}"
                        _log("PARSER", f"fallback verdict via editor-overall: {rest}")
                    break

    return {
        "task": task,
        "roles": role_outputs,
        "status": "gated",
        "verdict": verdict,
        "reason": reason,
    }


# ══════════════════════════════════════════════════════════════════════════
# Apply-engineer-output pipeline
#
# Turns the engineer role's LIRIL text output into an actual change on disk,
# then gates that change through a second LIRIL audit on the diff itself.
# Heavy safeguards: whitelisted paths only, known file extensions only,
# JSON-safety check, diff-audit PASS required before commit.
# ══════════════════════════════════════════════════════════════════════════

# Paths the dev team is allowed to modify. Anything outside this set is
# rejected before writes are attempted. Error on the side of refusal.
ALLOWED_PATH_PREFIXES = (
    "data/",
    "docs/",
    # HTML pages — specific list, not all *.html, to prevent the dev team
    # from rewriting shell.js loaders or navigation frames by accident.
    "enforcement-followthrough.html",
    "officer-of-parliament-findings.html",
    "instagram-draft-assist.html",
    "liril-dev-team.html",
    "accountability-inflections.html",
    "temporal-overlap.html",
)

# File extensions the dev team can write. Anything else (binary, secret,
# config) is rejected.
ALLOWED_EXTENSIONS = (".json", ".md", ".html", ".txt")


def path_is_allowed(path: str) -> tuple[bool, str]:
    """Return (ok, reason). Strict whitelist — refuse anything unusual."""
    p = path.replace("\\", "/").lstrip("/")
    if ".." in p.split("/"):
        return False, "path traversal not allowed"
    if p.startswith(".git") or "/.git/" in p or p.endswith(".env") or "/secret" in p:
        return False, "refuse: secrets / vcs / env"
    if not any(p.startswith(pre) for pre in ALLOWED_PATH_PREFIXES):
        return False, f"not in ALLOWED_PATH_PREFIXES"
    if not any(p.endswith(ext) for ext in ALLOWED_EXTENSIONS):
        return False, f"extension not in ALLOWED_EXTENSIONS"
    return True, ""


def extract_engineer_block(engineer_text: str) -> tuple[str, str]:
    """Pull the fenced code block out of the engineer role's output.
    Returns (language_hint, content). Falls back to full text if no fences.
    """
    lines = (engineer_text or "").splitlines()
    # Find the first ``` fence
    open_idx = None
    for i, raw in enumerate(lines):
        s = raw.strip()
        if s.startswith("```"):
            open_idx = i
            break
    if open_idx is None:
        # No fence — best-effort return
        return "", engineer_text.strip()
    lang = lines[open_idx].strip()[3:].strip().lower()
    # Find the closing ``` fence
    for j in range(open_idx + 1, len(lines)):
        if lines[j].strip().startswith("```"):
            return lang, "\n".join(lines[open_idx + 1:j])
    # No close found — return from open to end
    return lang, "\n".join(lines[open_idx + 1:])


def apply_engineer_output_to_json(target_path: Path, content: str, task_id: str) -> tuple[bool, str]:
    """Merge a JSON engineer output into an existing JSON target.

    Strict rules:
    - The engineer output must parse as a dict (not list/scalar).
    - It must not overwrite or remove existing top-level keys. Only add
      NEW top-level keys, or extend dict-valued keys with sub-merges.
    - All dates referenced in added content must look like ISO dates.

    Returns (changed, diff_text).
    """
    try:
        incoming = json.loads(content)
    except Exception as e:
        return False, f"[apply-reject] engineer output is not valid JSON: {e}"
    if not isinstance(incoming, dict):
        return False, "[apply-reject] engineer output is not a JSON object"

    existing = json.loads(target_path.read_text(encoding="utf-8"))
    added_keys = []
    mutated_keys = []
    for k, v in incoming.items():
        if k not in existing:
            existing[k] = v
            added_keys.append(k)
        elif isinstance(existing[k], dict) and isinstance(v, dict):
            for sk, sv in v.items():
                if sk not in existing[k]:
                    existing[k][sk] = sv
                    added_keys.append(f"{k}.{sk}")
        elif isinstance(existing[k], list) and isinstance(v, list):
            # Refuse list replacement. Allow append only if new items are new dicts.
            for item in v:
                if item not in existing[k]:
                    existing[k].append(item)
                    mutated_keys.append(f"{k}[+]")
        else:
            # Scalar or mismatched shape — refuse to overwrite.
            return False, f"[apply-reject] would overwrite existing key {k!r} (engineer cannot replace scalars)"

    if not added_keys and not mutated_keys:
        return False, "[apply-noop] engineer output added nothing new"

    # Serialize back with deterministic indent. Write atomically.
    new_text = json.dumps(existing, ensure_ascii=False, indent=2) + "\n"
    if new_text == target_path.read_text(encoding="utf-8"):
        return False, "[apply-noop] serialized result identical to existing file"
    target_path.write_text(new_text, encoding="utf-8")
    summary = f"added keys: {added_keys}; mutated: {mutated_keys}"
    return True, summary


def apply_engineer_output(task: dict, engineer_text: str) -> dict:
    """Dispatch to the right applier based on target file type.
    Returns {"ok", "reason", "target", "summary"} — ok=True means a file was written.
    """
    targets = task.get("target_files", [])
    if not targets:
        return {"ok": False, "reason": "task has no target_files"}
    # First version: only apply to the FIRST target (simpler, safer).
    target_rel = targets[0]
    ok, why = path_is_allowed(target_rel)
    if not ok:
        return {"ok": False, "reason": f"refusing target {target_rel!r}: {why}"}
    target_path = SITE / target_rel
    if not target_path.exists():
        return {"ok": False, "reason": f"target does not exist: {target_path}"}

    lang, content = extract_engineer_block(engineer_text)
    if not content.strip():
        return {"ok": False, "reason": "engineer produced empty output"}

    if target_rel.endswith(".json"):
        changed, summary = apply_engineer_output_to_json(target_path, content, task.get("id", "?"))
        return {"ok": changed, "reason": summary, "target": target_rel, "summary": summary}

    # For now, refuse non-JSON targets until we've stress-tested the JSON path.
    return {"ok": False, "reason": f"non-JSON target application not yet enabled ({target_rel})"}


async def diff_audit(nc, diff_text: str, task: dict) -> tuple[str, str]:
    """Second-pass LIRIL audit on the proposed diff itself.
    Returns (verdict, reason). PASS means the diff is safe to commit.
    """
    if len(diff_text) > 12000:
        diff_text = diff_text[:6000] + "\n...[truncated]...\n" + diff_text[-5000:]
    prompt = (
        "/no_think\n\n"
        "You are LIRIL, the TENET5 diff-audit gate. You review a git diff "
        "proposed by an autonomous dev team. You emit exactly two lines.\n\n"
        "VERDICT: PASS | WATCH | FAIL\n"
        "REASON: <one short sentence>\n\n"
        "PASS = diff is aligned with the task, adds only new content, "
        "introduces no contradictions with existing facts.\n"
        "WATCH = diff is plausible but firmer source anchoring would help.\n"
        "FAIL = diff contains a factual error, overwrites a verified claim, "
        "or goes beyond the task's scope.\n\n"
        f"Task: {task.get('title','')}\n"
        f"Acceptance: {task.get('acceptance','')}\n\n"
        "Proposed diff:\n"
        f"```\n{diff_text}\n```\n\n"
        "Respond now. Start with 'VERDICT:'.\nVERDICT:"
    )
    payload = json.dumps({
        "prompt": prompt,
        "max_tokens": 100,
        "temperature": 0.1,
        "stop": ["\n\n", "Explanation", "Okay"],
    }).encode("utf-8")
    try:
        msg = await nc.request("mercury.infer.code", payload, timeout=60)
    except Exception as e:
        return "ERROR", f"diff-audit infer failed: {e!r}"
    text = msg.data.decode("utf-8", errors="replace")
    try:
        envelope = json.loads(text)
        body = envelope.get("text", text)
    except Exception:
        body = text
    verdict = "UNPARSED"
    reason = ""
    for line in body.splitlines():
        L = line.strip()
        if L.upper().startswith("VERDICT:"):
            rest = L.split(":", 1)[1].strip()
            if rest:
                verdict = rest.split()[0].upper().strip(".,;:")
        elif L.upper().startswith("REASON:"):
            reason = L.split(":", 1)[1].strip()[:200]
    return verdict, reason


def git_diff_of_target(target_rel: str) -> str:
    import subprocess
    r = subprocess.run(
        ["git", "-C", str(SITE), "diff", "--no-color", "--", target_rel],
        capture_output=True, encoding="utf-8", errors="replace",
        creationflags=CREATE_NO_WINDOW,
    )
    return r.stdout or ""


def git_commit_if_clean(subject: str, body: str, paths: list[str], dry_run: bool) -> bool:
    """Commit given paths with a plain-English subject. Rejects jargon locally
    before even invoking git so the pre-commit hook doesn't need to.
    """
    for pat in BANNED_SUBJECT_PATTERNS:
        if pat.lower() in subject.lower():
            _log("GIT", f"refusing: subject contains banned pattern {pat!r}")
            return False
    if dry_run:
        _log("GIT", f"[dry-run] would commit: {subject}")
        return True
    site = str(SITE)
    # Stage only the specific paths the role pipeline produced
    for p in paths:
        subprocess.run(["git", "-C", site, "add", "--", p], check=False,
                       encoding="utf-8", errors="replace",
                       creationflags=CREATE_NO_WINDOW)
    # Abort if nothing staged
    diff = subprocess.run(["git", "-C", site, "diff", "--cached", "--quiet"],
                          encoding="utf-8", errors="replace",
                          creationflags=CREATE_NO_WINDOW)
    if diff.returncode == 0:
        _log("GIT", "nothing to commit (no staged diff)")
        return False
    msg = subject + "\n\n" + body + "\n\nCo-Authored-By: LIRIL Dev Team <liril@tenet5.local>\n"
    r = subprocess.run(["git", "-C", site, "commit", "-m", msg],
                       capture_output=True, encoding="utf-8", errors="replace",
                       creationflags=CREATE_NO_WINDOW)
    if r.returncode == 0:
        _log("GIT", f"committed: {subject}")
        return True
    _log("GIT", f"commit rejected by hook or error: {r.stderr.strip()[:300]}")
    return False


async def _auto_train_sample(task: dict) -> None:
    """Publish (task_text, axis_domain) to tenet5.liril.train so the
    NPU classifier learns from its own successful cycles. Async so it
    can be awaited from the daemon's running event loop — earlier
    sync version called asyncio.run() which fails inside an active
    loop with "asyncio.run() cannot be called from a running event
    loop". Errors swallowed — never blocks the cycle.
    """
    title = task.get("title", "")
    acceptance = (task.get("acceptance") or "").split(".")[0].strip()[:240]
    text = (title + ". " + acceptance).strip(". ").strip()
    domain = (task.get("axis_domain") or "").upper()
    if not text or not domain:
        return
    try:
        import nats as _nats  # noqa: WPS433
    except ImportError:
        return
    try:
        nc = await _nats.connect(os.environ.get("NATS_URL", "nats://127.0.0.1:4223"),
                                 connect_timeout=3)
        try:
            payload = json.dumps({"task": text, "domain": domain,
                                   "source": "dev_team_auto_train"}).encode()
            await nc.publish("tenet5.liril.train", payload)
            await nc.flush(timeout=3)
        finally:
            await nc.drain()
        _log("AUTO-TRAIN", f"fed classifier: {text[:60]} → {domain}")
    except Exception as e:
        _log("AUTO-TRAIN", f"publish failed: {e!r}")


def select_next_task(board: dict) -> dict | None:
    """Pick next task. Two paths:

    1. QAOA scheduler (default if LIRIL_USE_QAOA_SCHEDULER != '0')
       — uses tools/liril_pvsnp_scheduler.select_best which scores
       each pending task (difficulty, historical pass rate by axis,
       priority, category diversity) and solves a QUBO for the top
       pick. Falls back to greedy if pool > 14 tasks. Publishes
       decisions to tenet5.liril.scheduler.decisions.

    2. Fallback: priority-bucket FIFO scan.
    """
    pending = [t for t in board.get("backlog", [])
               if t.get("status") in ("pending", "needs_rework")]
    if not pending:
        return None

    if os.environ.get("LIRIL_USE_QAOA_SCHEDULER", "1") != "0":
        try:
            import liril_pvsnp_scheduler as _sched
            picks = _sched.select_best(pending, k=1, method="auto")
            if picks:
                winner_id = picks[0].get("task_id")
                for t in pending:
                    if t.get("id") == winner_id:
                        _log("SCHEDULER",
                             f"QAOA picked {winner_id} score={picks[0].get('score', '?'):.3f}")
                        return t
        except Exception as e:
            _log("SCHEDULER", f"QAOA fallback to FIFO: {type(e).__name__}: {e}")

    # Fallback: priority-bucket FIFO scan
    for priority in ("high", "medium", "low"):
        for task in board.get("backlog", []):
            if task.get("priority") == priority and task.get("status") == "pending":
                return task
    return None


def mark_task_in_progress(board: dict, task_id: str) -> None:
    task = next((t for t in board.get("backlog", []) if t.get("id") == task_id), None)
    if task is None:
        return
    task["status"] = "in_progress"
    task["started_at"] = _now_iso()
    board["in_progress"] = [t for t in board.get("in_progress", []) if t.get("id") != task_id]
    board["in_progress"].append(task)


def mark_task_done(board: dict, task_id: str, verdict: str, reason: str, committed: bool) -> None:
    board["backlog"] = [t for t in board.get("backlog", []) if t.get("id") != task_id]
    board["in_progress"] = [t for t in board.get("in_progress", []) if t.get("id") != task_id]
    entry = {
        "id": task_id,
        "finished_at": _now_iso(),
        "liril_verdict": verdict,
        "reason": reason,
        "committed": bool(committed),
    }
    board.setdefault("done_today", []).append(entry)
    stats = board.setdefault("stats", {})
    stats["completed_today"] = len(board["done_today"])
    total = stats.get("total_backlog", len(board["backlog"]) + stats["completed_today"])
    done_committed = len([e for e in board["done_today"] if e.get("committed")])
    stats["commit_pass_rate_pct"] = (
        round(done_committed / stats["completed_today"] * 100, 1)
        if stats["completed_today"] else None
    )
    stats["current_sprint_progress_pct"] = (
        round(stats["completed_today"] / total * 100, 1) if total else 0
    )


def mark_task_rejected(board: dict, task_id: str, verdict: str, reason: str) -> None:
    task = next((t for t in board.get("in_progress", []) if t.get("id") == task_id), None)
    if task is not None:
        task["status"] = "rejected"
        task["rejected_at"] = _now_iso()
        task["last_verdict"] = verdict
        task["last_reason"] = reason
        board["in_progress"] = [t for t in board.get("in_progress", []) if t.get("id") != task_id]
        # Put back on backlog so a human can decide whether to rework or drop
        for t in board.get("backlog", []):
            if t.get("id") == task_id:
                t["status"] = "needs_rework"
                t["last_verdict"] = verdict
                t["last_reason"] = reason
                return
        board.setdefault("recent_rejections", []).append({
            "id": task_id, "at": _now_iso(), "verdict": verdict, "reason": reason,
        })


async def _classify_confidence(nc, text: str) -> float:
    """Ask tenet5.liril.classify for a confidence score on a task. Returns
    0.0 on any error so a failing classifier does not block the cycle."""
    try:
        msg = await nc.request("tenet5.liril.classify",
                               json.dumps({"task": text}).encode(),
                               timeout=5)
        data = json.loads(msg.data.decode())
        return float(data.get("confidence", 0.0))
    except Exception:
        return 0.0


async def one_cycle(dry_run: bool = False) -> dict:
    board = load_board()
    task = select_next_task(board)
    if task is None:
        _log("CYCLE", "no pending task on backlog — nothing to do")
        return {"picked": None}

    # Pre-flight: LIRIL+TENET5 trilogue directive (2026-04-18).
    # Both orchestrator and 12B brain agreed that we should use the
    # NPU classifier's confidence score as a gating signal on the
    # dev-team. Low-confidence tasks are semantically ambiguous and
    # harder for a 12B model to handle. Skip them instead of burning
    # a cycle to likely UNPARSED.
    #
    # Threshold 0.15 chosen empirically: current classifier returns
    # 0.2-0.9 for real task descriptions; <0.15 is noise. We skip
    # instead of halt so the daemon picks the next candidate.
    MIN_CONFIDENCE = 0.15
    skipped_ids: list[str] = []
    picked = None
    for _try in range(3):  # try up to 3 candidates before giving up
        if task is None:
            break
        title = task.get("title", "")
        acceptance = task.get("acceptance", "")
        probe = (title + " " + acceptance).strip()
        if probe:
            try:
                nc_probe = await nats.connect(os.environ["NATS_URL"], connect_timeout=3)
                conf = await _classify_confidence(nc_probe, probe)
                await nc_probe.drain()
            except Exception:
                conf = 0.0
            if conf < MIN_CONFIDENCE:
                _log("CYCLE", f"skipping {task['id']} (classify confidence={conf:.3f} < {MIN_CONFIDENCE})")
                skipped_ids.append(task["id"])
                # Mark as needs_rework so it doesn't get re-picked immediately
                for t in board.get("backlog", []):
                    if t.get("id") == task["id"]:
                        t["status"] = "needs_rework"
                        t["last_verdict"] = "LOW_CONFIDENCE"
                        t["last_reason"] = f"NPU classify confidence {conf:.3f} below {MIN_CONFIDENCE}"
                        break
                save_board(board)
                task = select_next_task(board)
                continue
        picked = task
        break
    if picked is None:
        _log("CYCLE", f"no high-confidence tasks available (skipped {len(skipped_ids)})")
        return {"picked": None, "skipped_low_confidence": skipped_ids}
    task = picked

    _log("CYCLE", f"picked {task['id']} — {task['title']}")
    mark_task_in_progress(board, task["id"])
    save_board(board)

    nc = await nats.connect(os.environ["NATS_URL"])
    try:
        result = await execute_task(nc, task)
    finally:
        await nc.drain()

    verdict = result.get("verdict", "UNPARSED")
    reason = result.get("reason", "")
    _log("CYCLE", f"verdict={verdict} reason={reason!r}")

    # Save a per-task transcript for the public page
    transcript_path = LOG_DIR / f"{task['id']}_{int(time.time())}.json"
    transcript_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str),
                               encoding="utf-8")

    board = load_board()  # reload — may have been touched
    apply_info = None
    diff_verdict = None
    diff_reason = None
    if verdict == "PASS":
        # ── APPLY ENGINEER OUTPUT ────────────────────────────────────────
        # Find the engineer role's text in the pipeline transcript.
        engineer_out = None
        for r in result.get("roles", []):
            if r.get("role") == "engineer":
                engineer_out = r.get("text", "")
                break

        if engineer_out:
            apply_info = apply_engineer_output(task, engineer_out)
            _log("APPLY", f"{task['id']} → {apply_info.get('reason','')}")
        else:
            apply_info = {"ok": False, "reason": "no engineer role in pipeline"}
            _log("APPLY", f"{task['id']} → no engineer role, nothing to apply")

        # Guard against false-positive PASSes: if the task's pipeline
        # INCLUDED an engineer role but nothing was applied, treat the
        # task as needing rework. The gatekeeper judged role-text
        # coherence, not whether the acceptance criterion was actually met.
        false_positive = "engineer" in task.get("role_pipeline", []) and not apply_info.get("ok")
        if false_positive:
            _log("APPLY", f"false-positive PASS detected: engineer pipeline but no applied change")

        # ── DIFF AUDIT ───────────────────────────────────────────────────
        # If we actually wrote something, send the git diff back through
        # LIRIL for a second-pass audit before committing.
        staged_paths = [f"data/liril_dev_team_log/{transcript_path.name}",
                        "data/liril_work_schedule.json"]
        if apply_info and apply_info.get("ok"):
            target_rel = apply_info["target"]
            diff_text = git_diff_of_target(target_rel)
            nc2 = await nats.connect(os.environ["NATS_URL"])
            try:
                diff_verdict, diff_reason = await diff_audit(nc2, diff_text, task)
            finally:
                await nc2.drain()
            _log("DIFF-AUDIT", f"{task['id']} → {diff_verdict}  {diff_reason!r}")

            if diff_verdict == "PASS":
                staged_paths.insert(0, target_rel)
            else:
                # Revert the target file — we don't ship WATCH or FAIL diffs
                _log("DIFF-AUDIT", f"reverting {target_rel} — diff-audit not PASS")
                subprocess.run(
                    ["git", "-C", str(SITE), "checkout", "--", target_rel],
                    check=False, encoding="utf-8", errors="replace",
                    creationflags=CREATE_NO_WINDOW,
                )

        # ── BUILD COMMIT MESSAGE ─────────────────────────────────────────
        # 2026-04-18: compute commit_verdict BEFORE building the commit
        # body. The prior ordering hardcoded "verdict PASS" even when
        # diff-audit had reverted the target, producing commits that
        # claimed "added keys: ['reader_guidance']" when the file was
        # actually unchanged. Git log must not lie.
        if diff_verdict is not None and diff_verdict != "PASS":
            commit_verdict = f"PASS→{diff_verdict} (target reverted)"
        elif false_positive:
            commit_verdict = "PASS→WATCH (engineer emitted no change)"
        else:
            commit_verdict = "PASS"
        subject = f"autonomous({task['id']}): {task['title'][:60]}"
        body_parts = [
            f"LIRIL dev-team autonomous task — verdict {commit_verdict}",
            f"Reason: {reason}",
            f"Task: {task['title']}",
            f"Pipeline: {' → '.join(task.get('role_pipeline', []))}",
            f"Transcript: data/liril_dev_team_log/{transcript_path.name}",
        ]
        if apply_info:
            # If target was reverted, prefix the apply reason so the commit
            # message is unambiguous about the end-state (file unchanged).
            apply_line = apply_info.get('reason','')
            if diff_verdict is not None and diff_verdict != "PASS":
                apply_line = f"(reverted by diff-audit) {apply_line}"
            body_parts.append(f"Apply: {apply_line}")
            if apply_info.get("target"):
                body_parts.append(f"Target: {apply_info['target']}")
        if diff_verdict is not None:
            body_parts.append(f"Diff-audit: {diff_verdict}  {diff_reason}")
        body = "\n".join(body_parts) + "\n"

        committed = git_commit_if_clean(subject, body, staged_paths, dry_run)

        # Auto-train NPU on successful commits (LIRIL #D directive,
        # 2026-04-18 evening). Every PASS cycle is a labelled (task,
        # axis_domain) pair we can feed back to the classifier.
        # Fire-and-forget — errors swallowed, never block cycle.
        # 2026-04-18 update: only train on TRUE passes — don't reinforce
        # false-positive cycles where diff-audit reverted the target or
        # engineer emitted nothing applicable. Training on lies biases
        # the classifier toward noise-task patterns.
        real_pass = (committed
                     and (diff_verdict is None or diff_verdict == "PASS")
                     and not false_positive)
        if real_pass:
            try:
                await _auto_train_sample(task)
            except Exception as e:
                _log("AUTO-TRAIN", f"skipped: {e!r}")

        # Final outcome decision:
        # - PASS + apply ok + diff PASS  → mark done
        # - PASS + apply ok + diff WATCH/FAIL → mark rejected (target reverted above)
        # - PASS + engineer pipeline + no apply → false positive, mark rejected
        # - PASS + no engineer role → mark done (audit-only task is legitimate)
        effective_verdict = verdict
        effective_reason = reason
        if diff_verdict is not None and diff_verdict != "PASS":
            effective_verdict = f"PASS→{diff_verdict}"
            effective_reason = f"diff-audit {diff_verdict}: {diff_reason}"
            mark_task_rejected(board, task["id"], effective_verdict, effective_reason)
        elif false_positive:
            effective_verdict = "PASS→WATCH"
            effective_reason = f"engineer pipeline produced no applied change: {apply_info.get('reason','')}"
            mark_task_rejected(board, task["id"], effective_verdict, effective_reason)
        else:
            mark_task_done(board, task["id"], effective_verdict, effective_reason, committed)
    else:
        mark_task_rejected(board, task["id"], verdict, reason)

    save_board(board)
    return {
        "picked": task["id"],
        "verdict": verdict,
        "reason": reason,
        "apply": apply_info,
        "diff_verdict": diff_verdict,
        "diff_reason": diff_reason,
    }


def _auto_reset_stale_rework(min_age_minutes: int = 30) -> int:
    """Daemon self-maintenance (LIRIL option B): flip `needs_rework`
    tasks older than N minutes back to `pending` so they get retried.

    Previously this was a manual intervention — claude had to run
    reset_backlog_status.py after every ~5 cycles. Now the daemon
    does it itself before each cycle.

    Returns the number of tasks reset.
    """
    try:
        board = load_board()
    except Exception:
        return 0
    now_sec = time.time()
    reset = 0
    changed = False
    for t in board.get("backlog", []):
        if t.get("status") != "needs_rework":
            continue
        started = t.get("started_at") or t.get("rejected_at") or ""
        # Parse ISO8601; if unparseable, consider it old enough
        try:
            when = _dt.datetime.fromisoformat(started.rstrip("Z"))
            age_min = (now_sec - when.replace(tzinfo=_dt.timezone.utc).timestamp()) / 60
        except Exception:
            age_min = min_age_minutes + 1
        if age_min >= min_age_minutes:
            t["status"] = "pending"
            t.pop("last_verdict", None)
            t.pop("last_reason", None)
            reset += 1
            changed = True
    if changed:
        save_board(board)
    return reset


async def main_async(args: argparse.Namespace) -> None:
    if args.once:
        r = await one_cycle(dry_run=args.dry_run)
        print("\nCYCLE RESULT:", json.dumps(r, ensure_ascii=False, indent=2))
        return
    # daemon mode
    while True:
        # LIRIL option B: auto-reset stale needs_rework items so the
        # daemon can retry them instead of sitting idle waiting for a
        # human. 30-min age threshold means we aren't immediately
        # re-attempting failures — we give them a cool-off.
        try:
            reset = _auto_reset_stale_rework(min_age_minutes=30)
            if reset:
                _log("AUTO-RESET", f"flipped {reset} stale needs_rework → pending")
        except Exception as e:
            _log("AUTO-RESET", f"failed: {e!r}")
        try:
            r = await one_cycle(dry_run=args.dry_run)
            print("CYCLE RESULT:", json.dumps(r, ensure_ascii=False))
        except Exception as e:
            _log("ERROR", f"cycle raised: {e!r}")
        sleep_s = 60 * args.interval_min + random.randint(-60, 60)
        _log("DAEMON", f"sleeping {sleep_s}s until next cycle")
        await asyncio.sleep(sleep_s)


def main() -> None:
    ap = argparse.ArgumentParser(description="LIRIL autonomous dev-team cycle")
    ap.add_argument("--once", action="store_true", help="single cycle")
    ap.add_argument("--daemon", action="store_true", help="continuous loop")
    ap.add_argument("--dry-run", action="store_true", help="plan without committing")
    ap.add_argument("--interval-min", type=int, default=20, help="daemon cycle interval in minutes")
    args = ap.parse_args()
    if not args.once and not args.daemon:
        args.once = True
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
