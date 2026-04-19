# Copyright (c) 2024-2026 Daniel Perry. All Rights Reserved.
# Licensed under EOSL-2.0.
# Modified: 2026-04-18T02:48:00Z
"""LoRA fine-tuning driver for local AI coders.

Runs on the RTX 5070 Ti. Each role can be trained independently. Uses
unsloth for fast 4-bit LoRA fine-tuning of Mistral-Nemo-12B.

Dependencies (install once):
    pip install "unsloth[cu128-torch260] @ git+https://github.com/unslothai/unsloth.git"
    pip install peft transformers datasets

Usage:
    python tools/liril_train_lora.py --role engineer
    python tools/liril_train_lora.py --role gatekeeper --epochs 5
    python tools/liril_train_lora.py --all              # train every ready role

GPU budget per role:
    peak VRAM:     ~14 GB (LoRA rank=16 on 12B base)
    duration:      ~45 min for 200 examples, 3 epochs
    This blocks inference for the duration — pause the dev-team daemon first.
"""
from __future__ import annotations
import argparse
import json
import sys
import time
from pathlib import Path

LORA_DIR = Path(r"E:\S.L.A.T.E\tenet5\models\loras")
BASE_MODEL = Path(r"E:\S.L.A.T.E\tenet5\models\Mistral-Nemo-Instruct-2407-Q4_K_M.gguf")


def load_meta(role: str) -> dict:
    meta_path = LORA_DIR / role / "meta.json"
    if not meta_path.exists():
        raise FileNotFoundError(
            f"no training meta for role {role!r} at {meta_path}. "
            f"Run `python tools/liril_build_training_set.py` first."
        )
    return json.loads(meta_path.read_text(encoding="utf-8"))


def train_one(role: str, epochs: int, min_examples: int) -> bool:
    meta = load_meta(role)
    total = meta.get("examples_total", 0)
    if total < min_examples:
        print(f"[{role}] only {total} examples — need {min_examples}. skipping.")
        return False

    try:
        import torch
        from datasets import load_dataset
        from unsloth import FastLanguageModel
        from trl import SFTTrainer
        from transformers import TrainingArguments
    except ImportError as e:
        print(f"missing dependency: {e}. install unsloth + trl + datasets first.", file=sys.stderr)
        return False

    print(f"[{role}] loading base model ({BASE_MODEL.name}) ...")
    model, tokenizer = FastLanguageModel.from_pretrained(
        model_name=str(BASE_MODEL),
        max_seq_length=4096,
        dtype=None,
        load_in_4bit=True,
    )
    model = FastLanguageModel.get_peft_model(
        model,
        r=16,
        lora_alpha=32,
        lora_dropout=0.05,
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj"],
        bias="none",
        use_gradient_checkpointing=True,
    )

    dataset = load_dataset(
        "json",
        data_files=str(LORA_DIR / role / "train.jsonl"),
        split="train",
    )
    def format_chat(ex):
        return {"text": tokenizer.apply_chat_template(ex["messages"], tokenize=False)}
    dataset = dataset.map(format_chat)

    out_dir = LORA_DIR / role / f"v{int(time.time())}"
    out_dir.mkdir(parents=True, exist_ok=True)

    args = TrainingArguments(
        output_dir=str(out_dir),
        per_device_train_batch_size=4,
        gradient_accumulation_steps=2,
        num_train_epochs=epochs,
        learning_rate=2e-4,
        logging_steps=5,
        save_strategy="epoch",
        fp16=True,
        optim="adamw_8bit",
        seed=118400,
    )
    trainer = SFTTrainer(
        model=model,
        tokenizer=tokenizer,
        train_dataset=dataset,
        dataset_text_field="text",
        max_seq_length=4096,
        args=args,
    )
    print(f"[{role}] training {total} examples, {epochs} epochs ...")
    trainer.train()

    final_dir = out_dir / "final"
    model.save_pretrained(str(final_dir))
    tokenizer.save_pretrained(str(final_dir))

    # Update role meta
    meta["last_trained_at_utc"] = int(time.time())
    meta["last_lora_version"]   = final_dir.name
    meta["last_lora_path"]      = str(final_dir)
    (LORA_DIR / role / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"[{role}] trained. LoRA: {final_dir}")
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--role", help="single role to train")
    ap.add_argument("--all",  action="store_true", help="train every ready role")
    ap.add_argument("--epochs", type=int, default=3)
    ap.add_argument("--min-examples", type=int, default=50)
    args = ap.parse_args()

    if args.all:
        for role in ["researcher","architect","designer","engineer","editor","gatekeeper"]:
            try:
                train_one(role, args.epochs, args.min_examples)
            except FileNotFoundError as e:
                print(f"[{role}] {e}")
    elif args.role:
        train_one(args.role, args.epochs, args.min_examples)
    else:
        ap.error("need --role or --all")


if __name__ == "__main__":
    main()
