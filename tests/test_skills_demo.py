"""app/skills/demo.py's render_demo_blocks: the one deliberate exception to
Economy mode's "no LLM call per request" rule, run once at admin save-time.
A FakeClient stands in for the model, same pattern as test_pipeline.py."""

import asyncio
import json

import pytest

from app.llm.ollama_client import ChatResult, ModelUnavailable
from app.skills.demo import _ASK_NUDGE, _RECOVERY_BUDGET, render_demo_blocks
from app.skills.models import Function, SkillDoc

COMPLETE_WITH_BLOCKS = {
    "kind": "complete",
    "voice": "x",
    "message": "Here is the reference program.",
    "blocks": [
        {"name": "Stop robot", "slots": []},
    ],
    "note": None,
    "image_note": None,
}

PHASE_WITH_BLOCKS = {
    "kind": "phase",
    "voice": "x",
    "message": "Phase 1: set up.",
    "blocks": [
        {"name": "Stop robot", "slots": []},
    ],
    "phase_number": 1,
    "phase_total": 2,
    "note": None,
    "image_note": None,
}

ASK_NO_BLOCKS = {
    "kind": "ask",
    "voice": "x",
    "message": "Which sensor port?",
    "blocks": [],
    "note": None,
    "image_note": None,
}

# Caught live: a real save produced exactly this shape -- kind=complete,
# real (non-empty) blocks, yet every `if then`'s condition sat as a stray
# sibling block instead of seated in `conditions`, so render_tree rendered
# a visible red "expected 1 condition slot(s) for 'if then', got 0" box
# into what a coach is meant to be pre-approving as a clean reference.
#
# Deliberately a SLOT mismatch, not a condition-count one -- an empty
# `if then` condition used to be exactly this, but app/llm/pipeline.py's
# own retry (_is_condition_count_mismatch) now recovers that specific
# shape internally before render_demo_blocks's OWN separate retry
# mechanism (what these tests exercise) ever sees it. A slot mismatch on
# a plain leaf block isn't in that allowlist, so it still surfaces here
# as originally intended.
COMPLETE_WITH_A_STRUCTURAL_ERROR = {
    "kind": "complete",
    "voice": "x",
    "message": "Here is the reference program.",
    "blocks": [
        {"name": "Move robot's", "slots": [{"kind": "dropdown", "text": "Back axis"}]},
        {"name": "Stop robot", "slots": []},
    ],
    "note": None,
    "image_note": None,
}


class FakeClient:
    def __init__(self, responses):
        # A list consumed one response per call -- lets a test script a
        # kind=ask attempt followed by a kind=complete retry, or vice versa.
        self._responses = list(responses)
        self.calls = 0
        self.last_call = None

    async def chat(self, system, user, schema, image_base64=None, history=None, temperature=0):
        self.calls += 1
        self.last_call = {"system": system, "user": user, "schema": schema, "history": history}
        response = self._responses.pop(0)
        return ChatResult(text=json.dumps(response), model="fake-model")


class UnavailableClient:
    async def chat(self, system, user, schema, image_base64=None, history=None, temperature=0):
        raise ModelUnavailable("backend is down")


class RecursionErrorClient:
    """Stands in for the real failure caught live: a degenerate generation
    whose JSON is too deeply nested for json.loads to parse at all
    (app/llm/pipeline.py's answer() lets this propagate uncaught)."""

    def __init__(self):
        self.calls = 0

    async def chat(self, system, user, schema, image_base64=None, history=None, temperature=0):
        self.calls += 1
        # A real unparseable-depth string would be enormous; forcing the
        # actual error directly here keeps the test fast and deterministic.
        raise RecursionError("maximum recursion depth exceeded while decoding a JSON array")


def run(coro):
    return asyncio.run(coro)


def make_doc() -> SkillDoc:
    return SkillDoc(
        id="test-skill",
        name="Test Skill",
        functions=[Function(name="main_loop", body="stop the robot")],
    )


def test_a_clean_complete_response_renders_real_blocks():
    client = FakeClient([COMPLETE_WITH_BLOCKS])
    render = run(render_demo_blocks(make_doc(), client))
    assert render  # non-empty
    assert render[0]["p"] == [["t", "Stop robot"]]
    assert client.calls == 1


def test_a_kind_ask_response_is_retried_within_budget_then_gives_up():
    client = FakeClient([ASK_NO_BLOCKS] * (_RECOVERY_BUDGET + 1))
    render = run(render_demo_blocks(make_doc(), client))
    assert render == []
    assert client.calls == _RECOVERY_BUDGET + 1  # bounded, not an unbounded loop


def test_a_kind_ask_response_that_then_succeeds_on_retry_is_used():
    client = FakeClient([ASK_NO_BLOCKS, COMPLETE_WITH_BLOCKS])
    render = run(render_demo_blocks(make_doc(), client))
    assert render
    assert client.calls == 2


def test_a_phase_turn_is_followed_by_a_continue_reply_and_accumulates_blocks():
    # A long program builds phase by phase, same as a real student thread
    # (BuildAnswer's own docstring: "never dump the whole thing at once")
    # -- caught live: forcing kind=complete outright for a genuinely long
    # skill produced one giant generation that degenerated (a
    # RecursionError parsing the resulting JSON). Blocks from EVERY phase
    # must end up in the final render, not just the last one.
    client = FakeClient([PHASE_WITH_BLOCKS, COMPLETE_WITH_BLOCKS])
    render = run(render_demo_blocks(make_doc(), client))
    assert len(render) == 2  # one "Stop robot" from the phase, one from complete
    assert client.calls == 2
    # The second call carries the first phase's own turn as history, not a
    # repeat of the original long question.
    assert client.last_call["user"] != client.last_call["history"][0]["content"]
    assert len(client.last_call["history"]) == 2


def test_a_response_with_a_structural_validation_error_is_rejected_not_shipped():
    client = FakeClient([COMPLETE_WITH_A_STRUCTURAL_ERROR] * (_RECOVERY_BUDGET + 1))
    render = run(render_demo_blocks(make_doc(), client))
    # A confusing red-boxed "reference" is worse than the plain pseudocode
    # fallback (`[]`) that already worked before this feature existed.
    assert render == []
    assert client.calls == _RECOVERY_BUDGET + 1


def test_a_structural_error_that_is_fixed_on_retry_is_used():
    client = FakeClient([COMPLETE_WITH_A_STRUCTURAL_ERROR, COMPLETE_WITH_BLOCKS])
    render = run(render_demo_blocks(make_doc(), client))
    assert render
    assert client.calls == 2


def test_a_retry_after_a_structural_error_names_the_specific_error():
    client = FakeClient([COMPLETE_WITH_A_STRUCTURAL_ERROR, COMPLETE_WITH_BLOCKS])
    run(render_demo_blocks(make_doc(), client))
    assert "expected slots" in client.last_call["user"]


def test_a_recursion_error_bails_out_immediately_without_retrying():
    # Every call runs at temperature=0 -- retrying the identical question
    # would deterministically reproduce the same degenerate output, not
    # recover from it, so this must NOT consume the recovery budget and
    # try again.
    client = RecursionErrorClient()
    render = run(render_demo_blocks(make_doc(), client))
    assert render == []
    assert client.calls == 1


def test_model_unavailable_propagates_to_the_caller():
    # Deliberately NOT swallowed here -- app/api/main.py's save_skill is
    # the one that decides how to degrade (same as it already does for
    # compute_embedding being down).
    with pytest.raises(ModelUnavailable):
        run(render_demo_blocks(make_doc(), UnavailableClient()))


def test_the_skills_own_pseudocode_is_sent_to_the_model_directly():
    # render_demo_blocks embeds render_skill(doc) into the first question
    # itself rather than relying on retrieval matching (see the module's
    # own docstring) -- confirm the skill's real content actually made it
    # in, not just a reference to it.
    client = FakeClient([COMPLETE_WITH_BLOCKS])
    run(render_demo_blocks(make_doc(), client))
    assert "stop the robot" in client.last_call["user"]
    assert "main_loop" in client.last_call["user"]


def test_a_recovered_ask_sends_a_short_nudge_not_the_original_question_again():
    # The original long question is carried forward as conversation
    # history instead -- re-sending the whole skill doc as the "question"
    # every retry would be exactly the kind of oversized-turn pressure that
    # caused the real degenerate generation this module's docstring
    # describes.
    client = FakeClient([ASK_NO_BLOCKS, COMPLETE_WITH_BLOCKS])
    run(render_demo_blocks(make_doc(), client))
    assert client.last_call["user"] == _ASK_NUDGE
    assert client.last_call["history"][0]["role"] == "user"
    assert "main_loop" in client.last_call["history"][0]["content"]
