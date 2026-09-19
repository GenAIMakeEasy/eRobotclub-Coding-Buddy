"""app/prompts/store.py -- the JSON-backed persistence for admin-editable
prompt overrides. Mirrors tests/test_skills_store.py's own coverage shape
for the sibling module it's modeled on."""

from app.prompts.models import PromptOverrides
from app.prompts.store import load, save


def test_a_missing_file_loads_as_all_defaults(tmp_path):
    overrides = load(tmp_path / "does-not-exist.json")
    assert overrides == PromptOverrides()


def test_save_then_load_round_trips(tmp_path):
    path = tmp_path / "prompts.json"
    original = PromptOverrides(system_template="custom system", build_extra="custom build", debug_extra=None)
    save(original, path)
    loaded = load(path)
    assert loaded == original


def test_a_partial_override_leaves_the_other_fields_none(tmp_path):
    path = tmp_path / "prompts.json"
    save(PromptOverrides(system_template="only this one"), path)
    loaded = load(path)
    assert loaded.system_template == "only this one"
    assert loaded.build_extra is None
    assert loaded.debug_extra is None


def test_a_default_override_round_trips_too(tmp_path):
    path = tmp_path / "prompts.json"
    original = PromptOverrides(system_template_default="our team's baseline", build_extra=None)
    save(original, path)
    loaded = load(path)
    assert loaded == original


def test_effective_falls_through_active_then_default_then_hardcoded():
    hardcoded = "HARDCODED"
    assert PromptOverrides().effective("system_template", hardcoded) == hardcoded
    assert (
        PromptOverrides(system_template_default="TEAM DEFAULT").effective("system_template", hardcoded)
        == "TEAM DEFAULT"
    )
    assert (
        PromptOverrides(system_template="ACTIVE", system_template_default="TEAM DEFAULT").effective(
            "system_template", hardcoded
        )
        == "ACTIVE"
    )
