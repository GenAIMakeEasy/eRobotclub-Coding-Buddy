"""Economy mode serves a matched skill-doc directly, with zero LLM calls.
build_economy_answer is pure (no network); try_economy_answer's retrieval
step is exercised the same way app/skills/retrieve.py's own tests are
(FakeEmbedder / keyword fallback, no real network)."""

import asyncio

from app.llm.economy import build_economy_answer, try_economy_answer
from app.skills.models import Function, SkillDoc


def run(coro):
    return asyncio.run(coro)


def make_doc(usage_note="") -> SkillDoc:
    return SkillDoc(
        id="line-following",
        name="Line Following",
        triggers=["line"],
        functions=[
            Function(name="line_trace", body="if IR3 < IR:\n  turn left\nelse:\n  move forward", usage_note=usage_note),
            Function(name="TURN_LEFT", body="rotate left\nwait while IR6 > IR"),
        ],
    )


def test_message_uses_the_main_functions_usage_note():
    doc = make_doc(usage_note="Adjust IR and SPEED for your own track before running.")
    answer = build_economy_answer(doc)
    assert answer.message == "Adjust IR and SPEED for your own track before running."


def test_message_falls_back_to_a_plain_placeholder_when_no_usage_note_is_set():
    doc = make_doc(usage_note="")
    answer = build_economy_answer(doc)
    assert "Line Following" in answer.message
    assert "follow-up" in answer.message.lower()


def test_code_text_includes_every_functions_name_and_body_verbatim():
    doc = make_doc()
    answer = build_economy_answer(doc)
    assert "line_trace:" in answer.code_text
    assert "  if IR3 < IR:" in answer.code_text
    assert "TURN_LEFT:" in answer.code_text
    assert "  rotate left" in answer.code_text


def test_code_text_does_not_leak_the_model_only_instructions():
    # The student-facing code text must not contain the internal
    # MAIN LOOP/REUSABLE FUNCTIONS labelling or the chain-nesting warning
    # from app/skills/render.py -- those are instructions FOR the model,
    # not something a student needs to read.
    doc = make_doc()
    answer = build_economy_answer(doc)
    assert "MAIN LOOP" not in answer.code_text
    assert "REUSABLE FUNCTIONS" not in answer.code_text
    assert "Create a function" not in answer.code_text


def test_render_passes_through_the_docs_own_precomputed_demo_render():
    # No LLM call happens here -- build_economy_answer is pure (this
    # module's own docstring/tests) -- so `render` must come straight from
    # whatever app/skills/demo.py already computed and stored at save time,
    # not be recomputed or left behind.
    doc = make_doc()
    doc.demo_render = [{"id": "0", "cat": "Control", "p": [["t", "Forever"]]}]
    answer = build_economy_answer(doc)
    assert answer.render == doc.demo_render


def test_render_is_empty_for_a_skill_saved_before_this_existed():
    doc = make_doc()
    assert doc.demo_render == []
    answer = build_economy_answer(doc)
    assert answer.render == []


def test_answer_carries_the_skill_identity():
    doc = make_doc()
    answer = build_economy_answer(doc)
    assert answer.skill_id == "line-following"
    assert answer.skill_name == "Line Following"


def test_try_economy_answer_returns_none_when_nothing_matches():
    doc = make_doc()
    result = run(try_economy_answer("make it beep three times", [doc], embedder=None))
    assert result is None


def test_try_economy_answer_returns_none_with_no_skills_at_all():
    result = run(try_economy_answer("give code on line tracing", [], embedder=None))
    assert result is None


def test_try_economy_answer_matches_via_keyword_fallback():
    doc = make_doc(usage_note="Tune IR and SPEED.")
    result = run(try_economy_answer("give code on line tracing", [doc], embedder=None))
    assert result is not None
    assert result.skill_id == "line-following"
    assert result.message == "Tune IR and SPEED."
