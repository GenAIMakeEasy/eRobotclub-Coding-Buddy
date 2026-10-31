"""Reads and writes the admin-editable student levels (data/levels.json) --
the admin panel's CRUD surface (app/api/main.py's /admin/levels routes), the
public list the student entry screen's dropdown shows (/levels), and the
source /ask reads a level's prompt from.

A JSON file, not a database, same reasoning as app/skills/store.py and
app/prompts/store.py: a handful of entries, edited rarely, by one admin.
A missing file means "no levels yet" -- the dropdown simply doesn't show,
and the app behaves exactly like before this feature existed."""

from __future__ import annotations

import json
import re
import secrets
from dataclasses import dataclass, field
from pathlib import Path

from app import config

DEFAULT_PATH = config.REPO_ROOT / "data" / "levels.json"
DIRECTORY_PATH = config.REPO_ROOT / "data" / "directory_context.json"


@dataclass
class Level:
    id: str
    name: str
    prompt: str
    # Keys of the directory-context entries (below) this level sends the
    # model as its `{directory_context}`. Empty = the normal keyword routing.
    directory_keys: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "prompt": self.prompt, "directory_keys": self.directory_keys}


def load_all(path: Path = DEFAULT_PATH) -> list[Level]:
    if not path.exists():
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    return [
        Level(
            id=str(d["id"]),
            name=str(d.get("name", "")),
            prompt=str(d.get("prompt", "")),
            directory_keys=[str(k) for k in d.get("directory_keys", [])],
        )
        for d in raw.get("levels", [])
        if d.get("id")
    ]


def save_all(levels: list[Level], path: Path = DEFAULT_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"levels": [lv.to_dict() for lv in levels]}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def new_id(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:24] or "level"
    return f"{slug}-{secrets.token_hex(3)}"


# -- Directory context: the key/value text a level can pick from ------------
# The `{directory_context}` block of the system prompt is the R Block
# Directory's tabs (Control, Motion, ...). These entries are an admin-editable
# copy of them: key = tab name (or any name the admin adds), value = the text
# injected for it. Until an admin saves, they are seeded live from the
# vendored directory, so a fresh install sees the real tabs.
# NOTE: only the PROMPT text changes. The block names the model may emit
# are still enforced from the vendored directory (app/schema/).


def default_directory_entries() -> list[dict]:
    from app.schema import block_directory as bd

    return [{"key": k, "value": v} for k, v in bd.raw_tab_sections().items()]


def load_directory(path: Path = DIRECTORY_PATH) -> list[dict]:
    if not path.exists():
        return default_directory_entries()
    raw = json.loads(path.read_text(encoding="utf-8"))
    return [{"key": str(e["key"]), "value": str(e.get("value", ""))} for e in raw.get("entries", [])]


def save_directory(entries: list[dict], path: Path = DIRECTORY_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"entries": entries}, indent=2, ensure_ascii=False), encoding="utf-8")
