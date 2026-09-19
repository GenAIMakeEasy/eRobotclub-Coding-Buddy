"""The Session is what a student resumes; it must survive a save/load
round-trip, expire on schedule, and never let device_id touch a filesystem
path directly (CONTEXT.md: Device Identity is anonymous, untrusted input)."""

import time

from app.session.store import FileSessionStore


def test_a_new_device_has_no_session(tmp_path):
    store = FileSessionStore(directory=tmp_path, ttl_seconds=3600)
    assert store.load("device-abc") is None


def test_append_turn_creates_and_persists_a_session(tmp_path):
    store = FileSessionStore(directory=tmp_path, ttl_seconds=3600)
    store.append_turn("device-abc", {"role": "student", "question": "hi"}, language="中文")

    reloaded = store.load("device-abc")
    assert reloaded is not None
    assert reloaded.language == "中文"
    assert reloaded.turns == [{"role": "student", "question": "hi"}]


def test_turns_accumulate_across_calls(tmp_path):
    store = FileSessionStore(directory=tmp_path, ttl_seconds=3600)
    store.append_turn("device-abc", {"role": "student", "question": "one"})
    store.append_turn("device-abc", {"role": "assistant", "answer": "two"})

    reloaded = store.load("device-abc")
    assert len(reloaded.turns) == 2


def test_device_id_is_hashed_not_used_as_a_filename(tmp_path):
    store = FileSessionStore(directory=tmp_path, ttl_seconds=3600)
    dangerous_id = "../../etc/passwd"
    store.append_turn(dangerous_id, {"role": "student", "question": "hi"})

    files = list(tmp_path.glob("*.json"))
    assert len(files) == 1
    assert ".." not in files[0].name
    assert "/" not in files[0].name


def test_expired_session_is_treated_as_missing_and_deleted(tmp_path):
    store = FileSessionStore(directory=tmp_path, ttl_seconds=0.05)
    store.append_turn("device-abc", {"role": "student", "question": "hi"})
    time.sleep(0.1)

    assert store.load("device-abc") is None
    assert not list(tmp_path.glob("*.json"))


def test_sweep_expired_removes_only_stale_sessions(tmp_path):
    store = FileSessionStore(directory=tmp_path, ttl_seconds=0.05)
    store.append_turn("stale-device", {"role": "student", "question": "hi"})
    time.sleep(0.1)
    store.append_turn("fresh-device", {"role": "student", "question": "hi"})  # updated_at is now

    removed = store.sweep_expired()
    assert removed == 1
    remaining = list(tmp_path.glob("*.json"))
    assert len(remaining) == 1
    assert store.load("fresh-device") is not None


def test_a_corrupt_session_file_does_not_break_the_sweep(tmp_path):
    (tmp_path / "garbage.json").write_text("not json", encoding="utf-8")
    store = FileSessionStore(directory=tmp_path, ttl_seconds=3600)
    store.append_turn("device-abc", {"role": "student", "question": "hi"})

    removed = store.sweep_expired()
    assert removed == 0
    assert (tmp_path / "garbage.json").exists()
