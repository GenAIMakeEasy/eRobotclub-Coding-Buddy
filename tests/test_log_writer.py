"""The Log is permanent and raw (CONTEXT.md's Log entry) — it must never be
swept, and it must keep full text, not a summary, because a future local-AI
summarising pass needs raw material to work from (Q6)."""

import json

from app.session.log import LogWriter


def test_append_writes_one_json_line(tmp_path):
    log = LogWriter(directory=tmp_path)
    log.append({"device_id": "abc", "question": "how do I turn left"})

    files = list(tmp_path.glob("*.jsonl"))
    assert len(files) == 1
    line = files[0].read_text(encoding="utf-8").strip()
    record = json.loads(line)
    assert record["question"] == "how do I turn left"
    assert "logged_at" in record


def test_multiple_appends_accumulate_as_separate_lines(tmp_path):
    log = LogWriter(directory=tmp_path)
    log.append({"question": "one"})
    log.append({"question": "two"})

    files = list(tmp_path.glob("*.jsonl"))
    lines = files[0].read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 2
    assert json.loads(lines[0])["question"] == "one"
    assert json.loads(lines[1])["question"] == "two"


def test_raw_text_is_preserved_verbatim_not_summarised(tmp_path):
    log = LogWriter(directory=tmp_path)
    long_question = "why does my robot " + "keep spinning " * 20 + "at the corner"
    log.append({"question": long_question})

    files = list(tmp_path.glob("*.jsonl"))
    record = json.loads(files[0].read_text(encoding="utf-8").strip())
    assert record["question"] == long_question
