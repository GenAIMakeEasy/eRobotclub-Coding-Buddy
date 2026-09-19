"""SkillDoc's to_dict/from_dict round-trip is what the JSON file store
depends on -- a mismatch there would silently drop a teacher's edit on the
next load. store.py's CRUD is tested against tmp_path, never the real
data/skills.json."""

from pathlib import Path

from app.skills import store
from app.skills.models import Function, SkillDoc


def make_doc(id="line-following") -> SkillDoc:
    return SkillDoc(
        id=id,
        name="Line Following",
        triggers=["line", "junction"],
        intro="Use multiple sensors.",
        functions=[
            Function(name="line_trace", body="if IR3 < IR and IR4 > IR:\n  turn left\nelse:\n  move forward"),
            Function(name="FORWARD", body="move forward\nwait 0.15s"),
        ],
        embedding=[0.1, 0.2],
    )


def test_to_dict_and_from_dict_round_trip():
    doc = make_doc()
    restored = SkillDoc.from_dict(doc.to_dict())
    assert restored == doc


def test_demo_render_round_trips_through_to_dict_and_from_dict():
    doc = make_doc()
    doc.demo_render = [{"id": "0", "cat": "Control", "p": [["t", "Stop robot"]]}]
    restored = SkillDoc.from_dict(doc.to_dict())
    assert restored.demo_render == doc.demo_render


def test_from_dict_tolerates_missing_optional_fields():
    # A hand-edit (or a future schema change) that drops a field must not
    # crash callers -- degrade to sensible defaults instead.
    restored = SkillDoc.from_dict({"id": "x"})
    assert restored.id == "x"
    assert restored.name == "x"
    assert restored.functions == []


def test_load_all_on_a_missing_file_returns_empty():
    assert store.load_all(Path("/nonexistent/path/skills.json")) == []


def test_upsert_then_get_round_trips(tmp_path):
    path = tmp_path / "skills.json"
    doc = make_doc()
    store.upsert(doc, path=path)

    loaded = store.get("line-following", path=path)
    assert loaded is not None
    assert loaded.name == "Line Following"
    assert loaded.functions[0].name == "line_trace"
    assert loaded.updated_at  # stamped on save


def test_upsert_replaces_by_id_not_appends(tmp_path):
    path = tmp_path / "skills.json"
    store.upsert(make_doc(), path=path)
    updated = make_doc()
    updated.name = "Line Following v2"
    store.upsert(updated, path=path)

    all_skills = store.load_all(path)
    assert len(all_skills) == 1
    assert all_skills[0].name == "Line Following v2"


def test_upsert_preserves_other_skills(tmp_path):
    path = tmp_path / "skills.json"
    store.upsert(make_doc(id="a"), path=path)
    store.upsert(make_doc(id="b"), path=path)
    assert {s.id for s in store.load_all(path)} == {"a", "b"}


def test_delete_removes_only_the_matching_id(tmp_path):
    path = tmp_path / "skills.json"
    store.upsert(make_doc(id="a"), path=path)
    store.upsert(make_doc(id="b"), path=path)

    assert store.delete("a", path=path) is True
    assert {s.id for s in store.load_all(path)} == {"b"}
    assert store.delete("a", path=path) is False
