#!/usr/bin/env python3
"""Builds the knowledge pack that HQ hands out: a copy of the knowledge files, laid out exactly
like the repo, in to_share/ (plus knowledge-pack.zip). to_share/ is in .gitignore, so it never
goes to GitHub. Run it again whenever a knowledge file changes (for example after editing prompts
or skills in the admin pages).

    python3 scripts/make_share_pack.py

The real files stay where the app reads them; this only copies.
"""

import shutil
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "to_share"

FILES = [
    "vendor/r-block-directory.md",
    "vendor/r-block-directory.factory.md",
    "vendor/line-following-strategy.md",
    "data/skills.json",
    "CONTEXT.md",
]
DIRS = ["vendor/prompts", "docs/adr"]

README = """KNOWLEDGE PACK (private -- do not upload to GitHub)

What this is: the R Block knowledge the app needs, kept out of the public code.

How to use it:
1. Unzip knowledge-pack.zip into the root of the repo (the folder that contains `app/`).
   The folders line up: vendor/, vendor/prompts/, data/, docs/adr/.
2. Check:   ls vendor/r-block-directory.md vendor/prompts    and    python3 -m pytest -q
3. Restart the app. Admin -> Prompts shows the instructions in use.

Required: vendor/r-block-directory.md (do not edit it; its checksum is checked at startup).
Optional: everything else (see the repo README, section "Knowledge pack").

Rebuild this folder after changing a knowledge file:  python3 scripts/make_share_pack.py
"""


def main() -> None:
    if OUT.exists():
        shutil.rmtree(OUT)
    copied: list[str] = []
    for rel in FILES:
        src = ROOT / rel
        if not src.exists():
            print(f"  skipped (not found): {rel}")
            continue
        dst = OUT / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        copied.append(rel)
    for rel in DIRS:
        src = ROOT / rel
        if not src.is_dir():
            print(f"  skipped (not found): {rel}/")
            continue
        for f in sorted(p for p in src.rglob("*") if p.is_file()):
            copied.append(str(f.relative_to(ROOT)))
            dst = OUT / f.relative_to(ROOT)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, dst)
    (OUT / "README.txt").write_text(README, encoding="utf-8")
    with zipfile.ZipFile(OUT / "knowledge-pack.zip", "w", zipfile.ZIP_DEFLATED) as z:
        for rel in copied:
            z.write(OUT / rel, rel)
    print(f"to_share/: {len(copied)} files + README.txt + knowledge-pack.zip")
    for rel in copied:
        print("  ", rel)


if __name__ == "__main__":
    main()
