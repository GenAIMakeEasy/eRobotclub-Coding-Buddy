"""Reads and writes the admin-editable prompt overrides (data/prompts.json)
— the admin panel's CRUD surface (app/api/main.py's /admin/prompts routes)
and the source app/llm/pipeline.py's `answer()` reads from on every call.

A JSON file, not a database, same reasoning as app/skills/store.py: a
handful of fields, edited rarely, by one admin at a time. Missing file or
missing keys both mean "no override yet" (PromptOverrides' own all-None
default), so a fresh install with no data/prompts.json behaves exactly like
before this feature existed."""

from __future__ import annotations

import json
from pathlib import Path

from app import config
from app.prompts.models import PromptOverrides

DEFAULT_PATH = config.REPO_ROOT / "data" / "prompts.json"


def load(path: Path = DEFAULT_PATH) -> PromptOverrides:
    if not path.exists():
        return PromptOverrides()
    raw = json.loads(path.read_text(encoding="utf-8"))
    return PromptOverrides.from_dict(raw)


def save(overrides: PromptOverrides, path: Path = DEFAULT_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(overrides.to_dict(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
