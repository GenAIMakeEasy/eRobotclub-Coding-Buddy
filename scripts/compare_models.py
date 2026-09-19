#!/usr/bin/env python3
"""Run the same set of real prompts against several Ollama models and report
validation results + latency for each — so a model swap decision is based on
a repeatable comparison, not one-off manual runs.

Usage: python3 scripts/compare_models.py [model1] [model2] ...
Defaults to qwen3-vl:8b, gemma3:12b-it-qat, llama3.2-vision:11b.
"""

import asyncio
import base64
import sys
import time
import zlib
from pathlib import Path
from struct import pack

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.llm.ollama_client import OllamaClient, ModelUnavailable
from app.llm.pipeline import answer as run_pipeline


def _make_test_png() -> bytes:
    """A tiny synthetic black-line-on-white image, standing in for a real
    course-layout photo, so the vision path can be exercised without a real
    student's file."""

    def chunk(tag, data):
        c = tag + data
        return pack(">I", len(data)) + c + pack(">I", zlib.crc32(c))

    w, h = 64, 64
    rows = []
    for y in range(h):
        row = bytearray()
        for x in range(w):
            row += b"\x00\x00\x00" if 28 <= x <= 36 else b"\xff\xff\xff"
        rows.append(bytes(row))
    raw = b"".join(b"\x00" + r for r in rows)
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = chunk(b"IHDR", pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
    idat = chunk(b"IDAT", zlib.compress(raw))
    iend = chunk(b"IEND", b"")
    return sig + ihdr + idat + iend


TEST_IMAGE_B64 = base64.b64encode(_make_test_png()).decode()

CASES = [
    dict(
        name="simple build",
        mode="build",
        question="Make the robot move forward at speed 50, wait one second, then stop.",
    ),
    dict(
        name="nested build (if/else inside forever)",
        mode="build",
        question=(
            "My robot should follow the black line using the No.1 IR sensor. "
            "Loop forever: if the sensor value is greater than 200, turn right, "
            "otherwise keep going straight."
        ),
    ),
    dict(
        name="debug with a real fault",
        mode="debug",
        question=(
            "My robot should follow the black line using the No.1 IR sensor, but it "
            "never turns. It should check if the sensor value is greater than 200 and "
            "if so turn right, otherwise keep going straight. Repeat this forever."
        ),
    ),
    dict(
        name="vision (photo attached)",
        mode="build",
        question="This is a photo of the competition course. Describe the layout and tell me which block to use to follow the line shown.",
        image_base64=TEST_IMAGE_B64,
    ),
]


async def run_case(client: OllamaClient, case: dict) -> dict:
    t0 = time.monotonic()
    try:
        result = await run_pipeline(
            client=client,
            mode=case["mode"],
            question=case["question"],
            image_base64=case.get("image_base64"),
        )
        elapsed = time.monotonic() - t0
        return {
            "ok": result.ok,
            "elapsed": elapsed,
            "n_blocks": len(result.answer.blocks) if result.answer else 0,
            "kind": getattr(result.answer, "kind", None),
            "errors": [e.message for e in result.errors],
        }
    except ModelUnavailable as exc:
        return {"ok": False, "elapsed": time.monotonic() - t0, "error": str(exc)[:200]}


async def main(models: list[str]) -> None:
    for model in models:
        print(f"\n{'=' * 70}\n{model}\n{'=' * 70}")
        client = OllamaClient(model=model, timeout_seconds=180)
        try:
            await client.warmup()
        except Exception as exc:
            print(f"  (warmup failed: {exc}) — is this model pulled?")
            continue

        for case in CASES:
            result = await run_case(client, case)
            status = "OK  " if result.get("ok") else "FAIL"
            line = f"  [{status}] {case['name']:<40} {result.get('elapsed', 0):5.1f}s"
            if "n_blocks" in result:
                line += f"  blocks={result['n_blocks']}"
            if result.get("kind"):
                line += f"  kind={result['kind']}"
            print(line)
            if result.get("errors"):
                for e in result["errors"]:
                    print(f"         ! {e}")
            if result.get("error"):
                print(f"         ! {result['error']}")


if __name__ == "__main__":
    models = sys.argv[1:] or ["qwen3-vl:8b", "gemma3:12b-it-qat", "llama3.2-vision:11b"]
    asyncio.run(main(models))
