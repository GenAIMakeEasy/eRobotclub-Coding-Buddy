"""The Log: a permanent, append-only, raw record of every Enquiry and its
answer — kept for later study of what students struggle with. Never swept,
unlike the Session (see CONTEXT.md's Log entry, and the grilling session's
Q6: written raw because a future local-AI summarising pass needs full text
to work from, not a pre-compressed version).

Rotated daily for file-size manageability; rotation is not deletion — old
files are left in place indefinitely.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app import config


class LogWriter:
    def __init__(self, directory: Path = config.LOG_DIR):
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)

    def _path_for_today(self) -> Path:
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return self.directory / f"{day}.jsonl"

    def append(self, record: dict[str, Any]) -> None:
        record = {"logged_at": time.time(), **record}
        line = json.dumps(record, ensure_ascii=False)
        with self._path_for_today().open("a", encoding="utf-8") as f:
            f.write(line + "\n")
