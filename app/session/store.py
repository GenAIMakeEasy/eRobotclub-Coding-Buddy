"""The Session: what a student resumes. Per-device, cached to a file, swept
after 7 days (Q4/Q6) — distinct from the Log, which is permanent (see
app/session/log.py and CONTEXT.md's Session vs Log entries).

Device Identity is treated as opaque, untrusted client input, so it is
hashed into the filename rather than used directly — it must never be
interpretable as a path.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

from app import config


@dataclass
class Session:
    device_id: str
    language: str = "English"
    turns: list[dict[str, Any]] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


def _filename(device_id: str) -> str:
    digest = hashlib.sha256(device_id.encode("utf-8")).hexdigest()
    return f"{digest}.json"


class FileSessionStore:
    def __init__(self, directory: Path = config.SESSION_DIR, ttl_seconds: float = config.SESSION_TTL_SECONDS):
        self.directory = directory
        self.ttl_seconds = ttl_seconds
        self.directory.mkdir(parents=True, exist_ok=True)

    def _path(self, device_id: str) -> Path:
        return self.directory / _filename(device_id)

    def load(self, device_id: str) -> Optional[Session]:
        path = self._path(device_id)
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        if time.time() - data["updated_at"] > self.ttl_seconds:
            path.unlink(missing_ok=True)
            return None
        return Session(**data)

    def save(self, session: Session) -> None:
        session.updated_at = time.time()
        path = self._path(session.device_id)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(session)), encoding="utf-8")
        tmp.replace(path)

    def get_or_create(self, device_id: str, language: str = "English") -> Session:
        session = self.load(device_id)
        if session is not None:
            return session
        return Session(device_id=device_id, language=language)

    def append_turn(self, device_id: str, turn: dict[str, Any], language: Optional[str] = None) -> Session:
        session = self.get_or_create(device_id, language or "English")
        if language:
            session.language = language
        session.turns.append(turn)
        self.save(session)
        return session

    def sweep_expired(self) -> int:
        """Delete session files past their TTL. Returns how many were removed."""
        removed = 0
        now = time.time()
        for path in self.directory.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if now - data["updated_at"] > self.ttl_seconds:
                    path.unlink(missing_ok=True)
                    removed += 1
            except (json.JSONDecodeError, KeyError, OSError):
                # A corrupt or half-written file shouldn't block the sweep of
                # everything else; leave it for manual inspection.
                continue
        return removed
