"""Reads and writes the teacher-editable skill-doc library (data/skills.json)
— the admin panel's CRUD surface (app/api/main.py's /admin/skills routes)
and the source app/skills/retrieve.py searches over.

A JSON file, not a database: a handful of documents, edited rarely, by one
admin at a time — the same reasoning as .env for admin settings
(app/config.py's read/write_dotenv_*). Round-trips through the SkillDoc
dataclass (app/skills/models.py) so a hand-edit of the file that drops a
field doesn't crash callers.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from app import config
from app.skills.models import SkillDoc

DEFAULT_PATH = config.REPO_ROOT / "data" / "skills.json"


def load_all(path: Path = DEFAULT_PATH) -> list[SkillDoc]:
    if not path.exists():
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    return [SkillDoc.from_dict(d) for d in raw.get("skills", [])]


def save_all(skills: list[SkillDoc], path: Path = DEFAULT_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"skills": [s.to_dict() for s in skills]}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def get(skill_id: str, path: Path = DEFAULT_PATH) -> Optional[SkillDoc]:
    return next((s for s in load_all(path) if s.id == skill_id), None)


def upsert(doc: SkillDoc, path: Path = DEFAULT_PATH) -> None:
    """Insert or replace by id, preserving every other skill-doc's order."""
    doc.updated_at = datetime.now(timezone.utc).isoformat()
    skills = load_all(path)
    for i, s in enumerate(skills):
        if s.id == doc.id:
            skills[i] = doc
            break
    else:
        skills.append(doc)
    save_all(skills, path)


def delete(skill_id: str, path: Path = DEFAULT_PATH) -> bool:
    skills = load_all(path)
    remaining = [s for s in skills if s.id != skill_id]
    if len(remaining) == len(skills):
        return False
    save_all(remaining, path)
    return True
