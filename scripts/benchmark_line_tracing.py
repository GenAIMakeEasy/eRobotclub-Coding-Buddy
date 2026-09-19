#!/usr/bin/env python3
"""Accuracy benchmark for the specific case flagged as needing to match the
vendored reference pattern (vendor/line-following-strategy.md's worked route
example): a full line-tracer that detects junctions with the No.5/No.6
sensor pair, counts them, dispatches a route by count, and falls back to
No.3/No.4 side-sensor steering the rest of the time.

Drives a REAL multi-turn conversation (ask -> plan confirm -> phase(s) ->
complete) through the actual pipeline, exactly as a student would, then
checks the accumulated final blocks for the structural markers a correct
answer must have. This is a shape check (does the right kind of logic
exist, correctly connected), not a byte-for-byte match to the reference.

Usage: python3 scripts/benchmark_line_tracing.py [model1] [model2] ...
Model names ending in nothing recognizable as Ollama fall back to Gemini
(pass "gemini" to test that backend).
"""

import asyncio
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.llm.gemini_client import GeminiClient
from app.llm.ollama_client import ModelUnavailable, OllamaClient
from app.llm.pipeline import answer as run_pipeline, summarize_blocks
from app.schema.answer_contract import ProgramBlock
from app.schema.validate import validate_answer

QUESTION = (
    "Make my robot go around a square track with corners, following the black "
    "line, and stop after it has gone around once."
)

REPLIES = [
    "Yes, that's right, please build it.",
    "I'm ready for the next phase.",
    "I'm ready for the next phase.",
    "I'm ready for the next phase.",
    "I'm ready for the next phase.",
]

MAX_TURNS = 8


def flatten_names(blocks: list[ProgramBlock], out: list[str]) -> None:
    for b in blocks:
        out.append(b.name.value)
        flatten_names(b.conditions, out)
        flatten_names(b.children, out)
        flatten_names(b.else_children, out)


def flatten_source(blocks: list[ProgramBlock]) -> str:
    """A crude but effective text rendering of the whole tree (names +
    slot values), good enough to regex-search for sensor-pair patterns
    regardless of exact nesting."""
    parts: list[str] = []

    def walk(block_list, depth=0):
        for b in block_list:
            slots = " ".join(s.text for s in b.slots)
            parts.append(f"{b.name.value} {slots}")
            walk(b.conditions, depth + 1)
            walk(b.children, depth + 1)
            walk(b.else_children, depth + 1)

    walk(blocks)
    return "\n".join(parts)


def make_client(name: str):
    if name == "gemini":
        return GeminiClient()
    return OllamaClient(model=name, timeout_seconds=180)


async def run_conversation(client) -> tuple[list[ProgramBlock], list, list[dict]]:
    """Drives ask -> phase* -> complete, accumulating every block seen
    across all phases (a phase only carries ITS OWN blocks, not the whole
    program). Returns (all_blocks, all_errors, transcript_log)."""
    history: list[dict] = []
    all_blocks: list[ProgramBlock] = []
    all_errors = []
    log = []

    question = QUESTION
    for turn_i in range(MAX_TURNS):
        result = await run_pipeline(client, "build", question, history=history)
        log.append({"turn": turn_i, "kind": result.answer.kind if result.answer else None, "errors": [e.message for e in result.errors]})

        if not result.answer:
            break
        all_errors.extend(result.errors)
        all_blocks.extend(result.answer.blocks)

        history.append({"role": "user", "content": question})
        content = result.answer.message
        if result.answer.blocks:
            content += "\nBlocks so far: " + summarize_blocks(result.answer.blocks)
        history.append({"role": "assistant", "content": content})

        if result.answer.kind == "complete":
            break
        if turn_i >= len(REPLIES):
            break
        question = REPLIES[turn_i]

    return all_blocks, all_errors, log


MARKERS = {
    "Forever loop": lambda src: "Forever" in src,
    "Junction check (No.5 AND No.6 together)": lambda src: bool(
        re.search(r"If the IR sensor value No\.5.*No\.6|If the IR sensor value No\.6.*No\.5", src, re.S)
    )
    or ("No.5" in src and "No.6" in src and "if then" in src),
    "Counter variable increment": lambda src: "Add the variable" in src,
    "Route/count dispatch (nested if on the counter)": lambda src: src.count("if then") >= 2,
    "Side-sensor steering (No.3 or No.4)": lambda src: "No.3" in src or "No.4" in src,
    "Fallback forward movement": lambda src: "Forward" in src or "Move robot" in src,
}


async def benchmark(model_name: str) -> None:
    print(f"\n{'=' * 70}\n{model_name}\n{'=' * 70}")
    client = make_client(model_name)
    if hasattr(client, "warmup"):
        try:
            await client.warmup()
        except Exception as exc:
            print(f"  (warmup failed: {exc})")
            return

    t0 = time.monotonic()
    try:
        blocks, errors, log = await run_conversation(client)
    except ModelUnavailable as exc:
        print(f"  FAILED: {exc}")
        return
    elapsed = time.monotonic() - t0

    print(f"  elapsed: {elapsed:.1f}s, turns: {len(log)}, total blocks: {len(blocks)}")
    for entry in log:
        print(f"    turn {entry['turn']}: kind={entry['kind']} errors={entry['errors']}")

    src = flatten_source(blocks)
    print(f"\n  Structural accuracy vs. the reference pattern:")
    hits = 0
    for label, check in MARKERS.items():
        ok = check(src)
        hits += ok
        print(f"    [{'OK  ' if ok else 'MISS'}] {label}")
    print(f"  Score: {hits}/{len(MARKERS)}")

    real_errors = [e for e in errors]
    print(f"  Validation errors across the whole conversation: {len(real_errors)}")
    for e in real_errors:
        print(f"    ! {e.message}")


async def main(models: list[str]) -> None:
    for m in models:
        await benchmark(m)


if __name__ == "__main__":
    models = sys.argv[1:] or ["gemma4:12b-it-qat", "gemini"]
    asyncio.run(main(models))
