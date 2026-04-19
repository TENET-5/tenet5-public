# Copyright (c) 2024-2026 Daniel Perry. All Rights Reserved.
# Licensed under EOSL-2.0.
# Modified: 2026-04-18T02:45:00Z
"""Compile data/liril_corrections.jsonl into per-role training datasets.

Output layout:
  E:/S.L.A.T.E/tenet5/models/loras/<role>/train.jsonl
  E:/S.L.A.T.E/tenet5/models/loras/<role>/meta.json

Training format is OpenAI-chat-compatible JSONL, which unsloth + peft
accept natively:
  {"messages": [
     {"role": "system", "content": "<role spec>"},
     {"role": "user",   "content": "<role input>"},
     {"role": "assistant", "content": "<correct output>"}
  ]}

Negative-label corrections get ALSO emitted as a contrastive example —
the bad output paired with a short rejection annotation, so the model
learns what NOT to produce.
"""
from __future__ import annotations
import json
import time
from pathlib import Path

SITE = Path(r"E:\TENET-5.github.io")
CORRECTIONS = SITE / "data" / "liril_corrections.jsonl"
LORA_DIR = Path(r"E:\S.L.A.T.E\tenet5\models\loras")

ROLE_SYSTEM_PROMPTS = {
    "researcher": (
        "You are the RESEARCHER on a TENET5 dev team. Your job is to identify "
        "exactly one specific primary-source fact needed to complete the task. "
        "Output SOURCE, QUOTE, GAP_FILLED triple. Cite Canada Gazette, Hansard, "
        "CanLII, or Commissioner reports. Never invent citations."
    ),
    "architect": (
        "You are the ARCHITECT on a TENET5 dev team. Your job is to name the "
        "exact target file and insertion point for a change. Output FILE, "
        "ANCHOR, POSITION. Respect the 19-axis / IA-pillar layout."
    ),
    "designer": (
        "You are the DESIGNER on a TENET5 dev team. Your job is to propose "
        "the UX/visual form of a change. Output FORM, ACCESSIBILITY, COPY_TONE. "
        "Follow Shneiderman overview-first, NN/g readability, WCAG 2.1 AA."
    ),
    "engineer": (
        "You are the ENGINEER on a TENET5 dev team. Your job is to produce "
        "the literal string to insert into a target file. Output a fenced "
        "code block only. No explanation, no narration, only the change."
    ),
    "editor": (
        "You are the EDITOR on a TENET5 dev team. Your job is to copy-check "
        "the engineer output. Output CHECK_CITATIONS, CHECK_LINKS, CHECK_A11Y, "
        "OVERALL (CLEAN | NEEDS_REWORK)."
    ),
    "gatekeeper": (
        "You are the GATEKEEPER on a TENET5 dev team — the hallucination gate. "
        "Output VERDICT: PASS | WATCH | FAIL, and REASON: one short sentence. "
        "FAIL if the pipeline produced no concrete deliverable when the task's "
        "acceptance criterion required one. WATCH if a date or citation looks "
        "slightly off but uncertain. PASS only when the diff is verifiable."
    ),
}


def load_corrections() -> list[dict]:
    if not CORRECTIONS.exists():
        return []
    out = []
    with CORRECTIONS.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except Exception:
                    pass
    return out


def build_example(entry: dict) -> dict | None:
    role = entry.get("role")
    if role not in ROLE_SYSTEM_PROMPTS:
        return None
    label = entry.get("training_label", "neutral")
    correct = entry.get("correct_output", "")
    if not correct:
        return None
    user_msg = entry.get("role_input", "").strip()
    if not user_msg:
        return None
    ex = {
        "messages": [
            {"role": "system",    "content": ROLE_SYSTEM_PROMPTS[role]},
            {"role": "user",      "content": user_msg},
            {"role": "assistant", "content": correct},
        ],
        "meta": {
            "entry_id":       entry.get("entry_id"),
            "training_label": label,
            "severity":       entry.get("severity", "none"),
            "task_id":        entry.get("task_id"),
            "annotation":     entry.get("annotation", ""),
        }
    }
    return ex


def main():
    entries = load_corrections()
    by_role: dict[str, list[dict]] = {r: [] for r in ROLE_SYSTEM_PROMPTS}
    for e in entries:
        ex = build_example(e)
        if ex is not None:
            by_role[e["role"]].append(ex)

    LORA_DIR.mkdir(parents=True, exist_ok=True)
    ts = int(time.time())
    summary = {}
    for role, examples in by_role.items():
        role_dir = LORA_DIR / role
        role_dir.mkdir(parents=True, exist_ok=True)
        train_path = role_dir / "train.jsonl"
        meta_path  = role_dir / "meta.json"

        with train_path.open("w", encoding="utf-8") as f:
            for ex in examples:
                # Strip 'meta' from the training line — unsloth expects just 'messages'.
                f.write(json.dumps({"messages": ex["messages"]}, ensure_ascii=False) + "\n")

        pos = sum(1 for ex in examples if ex["meta"]["training_label"] == "positive")
        neg = sum(1 for ex in examples if ex["meta"]["training_label"] == "negative")
        neu = len(examples) - pos - neg
        ready = len(examples) >= 50  # min_examples_to_train from registry

        meta = {
            "role":              role,
            "examples_total":    len(examples),
            "positive":          pos,
            "negative":          neg,
            "neutral":           neu,
            "ready_to_train":    ready,
            "min_required":      50,
            "generated_at_utc":  ts,
            "train_jsonl":       str(train_path),
            "examples_meta":     [ex["meta"] for ex in examples],
        }
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        summary[role] = {"total": len(examples), "pos": pos, "neg": neg, "ready": ready}

    print(f"compiled {sum(s['total'] for s in summary.values())} training examples from {CORRECTIONS.name}")
    for role in ROLE_SYSTEM_PROMPTS:
        s = summary[role]
        ready = " READY" if s["ready"] else f" (need {50 - s['total']} more)"
        print(f"  {role:14s}  total={s['total']:3d}  pos={s['pos']:3d}  neg={s['neg']:3d}  {ready}")


if __name__ == "__main__":
    main()
