"""The pipeline is where routing, the schema, and validation meet. A fake
ChatClient stands in for Ollama so these tests exercise the wiring — not the
model — and don't require a running backend.

Build follows the vendored skill's ask -> confirm -> phase-by-phase protocol
(see pipeline.py's docstring); Debug stays single-shot."""

import asyncio
import json

import pytest

from app.llm.ollama_client import ChatResult, ModelUnavailable
from app.llm.pipeline import (
    DEFAULT_BUILD_EXTRA,
    DEFAULT_SYSTEM_TEMPLATE,
    answer as run_pipeline,
    validate_prompt_override,
)
from app.prompts.models import PromptOverrides
from app.schema.answer_contract import Slot
from app.skills.models import SkillDoc


class FakeClient:
    def __init__(self, response_dict: dict | None = None, raw_text: str | None = None):
        self.response_dict = response_dict
        self.raw_text = raw_text
        self.last_call = None
        self.calls = 0

    async def chat(self, system, user, schema, image_base64=None, history=None, temperature=0):
        self.calls += 1
        self.last_call = {
            "system": system,
            "user": user,
            "schema": schema,
            "image_base64": image_base64,
            "history": history,
        }
        text = self.raw_text if self.raw_text is not None else json.dumps(self.response_dict)
        return ChatResult(text=text, model="fake-model")


VALID_BUILD_RESPONSE = {
    "kind": "complete",
    "voice": "All done!",
    "message": "Drive forward and stop.",
    "blocks": [
        {
            "name": "Move robot's",
            "slots": [
                {"kind": "dropdown", "text": "Back axis"},
                {"kind": "dropdown", "text": "Forward"},
                {"kind": "value", "text": "50"},
            ],
        },
        {"name": "Stop robot", "slots": []},
    ],
    "note": None,
    "image_note": None,
}


def run(coro):
    return asyncio.run(coro)


def test_a_valid_response_produces_an_ok_result():
    client = FakeClient(response_dict=VALID_BUILD_RESPONSE)
    result = run(run_pipeline(client, "build", "make it drive forward and stop"))
    assert result.ok
    assert result.answer.kind == "complete"
    assert result.answer.message == "Drive forward and stop."
    assert len(result.answer.blocks) == 2


def test_an_oversized_message_is_truncated_instead_of_crashing():
    # Caught live against Gemini: its structured-output enforcement is
    # looser than Ollama's true grammar-constrained decoding, and it let a
    # `message` string through well past the schema's maxLength. That used
    # to hard-fail Pydantic validation into a ModelUnavailable — a student
    # just saw "something went wrong" for an answer that was otherwise
    # fine.
    long_message = "x" * 800
    client = FakeClient(response_dict={**VALID_BUILD_RESPONSE, "message": long_message})
    result = run(run_pipeline(client, "build", "make it drive forward and stop"))
    assert result.ok
    assert len(result.answer.message) == 700
    assert result.answer.message.endswith("…")


def test_an_oversized_optional_field_is_also_truncated():
    # note/image_note are Optional[str] -- maxLength sits under `anyOf` in
    # the JSON Schema for a nullable field, not directly on the property,
    # so this exercises that branch specifically.
    client = FakeClient(response_dict={**VALID_BUILD_RESPONSE, "note": "y" * 200})
    result = run(run_pipeline(client, "build", "make it drive forward and stop"))
    assert result.ok
    assert len(result.answer.note) == 140


def test_a_collapsed_block_name_is_repaired_instead_of_crashing():
    # Caught live against OpenRouter (google/gemma-4-26b-a4b-it:free):
    # `strict: true` on response_format didn't stop it from returning
    # `name: 'Run function " FORWARD "'` -- the catalog's own Exact Text
    # with the dropdown already filled in, collapsed into one string,
    # instead of `name: "Run function"` + a separate `slots` entry. That
    # used to hard-fail into "model output did not match the Answer
    # Contract".
    collapsed = {
        **VALID_BUILD_RESPONSE,
        "blocks": [
            {"name": 'Run function " FORWARD "', "slots": [], "children": [{"name": "Stop robot", "slots": []}]}
        ],
    }
    client = FakeClient(response_dict=collapsed)
    result = run(run_pipeline(client, "build", "call the FORWARD function"))
    assert result.ok, result.errors
    block = result.answer.blocks[0]
    assert block.name.value == "Run function"
    assert block.slots == [Slot(kind="dropdown", text="FORWARD")]


def test_a_collapsed_block_name_is_repaired_at_any_nesting_depth():
    collapsed = {
        **VALID_BUILD_RESPONSE,
        "blocks": [
            {
                "name": "if then",
                "slots": [],
                "conditions": [
                    {"name": "If the No.3 IR sensor value < 200", "slots": []},
                ],
                "children": [
                    {
                        "name": 'Run function " FORWARD "',
                        "slots": [],
                        "children": [{"name": "Stop robot", "slots": []}],
                    }
                ],
            }
        ],
    }
    client = FakeClient(response_dict=collapsed)
    result = run(run_pipeline(client, "build", "x"))
    assert result.ok, result.errors
    block = result.answer.blocks[0]
    assert block.conditions[0].name.value == "If the IR sensor value"
    assert block.children[0].name.value == "Run function"


def test_an_unanchored_block_name_pattern_is_not_falsely_repaired():
    # A real regression: catalog blocks whose Exact Text is nothing but
    # placeholders and bare spaces (e.g. "Comparison Operator" is
    # "[Value] [Dropdown] [Value]") produce an unanchored regex that would
    # otherwise match almost any multi-word string. A genuinely invented
    # block name must still be rejected, not silently "repaired" into a
    # wrong real block.
    invented = {**VALID_BUILD_RESPONSE, "blocks": [{"name": "Wait until line sensor is Black", "slots": []}]}
    client = FakeClient(response_dict=invented)
    with pytest.raises(ModelUnavailable, match="did not match the Answer Contract"):
        run(run_pipeline(client, "build", "x"))


def test_a_variable_reference_emitted_as_a_dropdown_slot_is_repaired():
    # Caught live: asked to compare a variable against a literal ("if
    # count == 1"), the model emitted Comparison Operator's first slot as
    # kind="dropdown" (reasonable-looking, since PICKING a variable to
    # update elsewhere in the catalog really is a dropdown choice) instead
    # of kind="value" -- which used to hard-fail validation with "expected
    # slots ['value', 'dropdown', 'value'] ... got ['dropdown', 'dropdown',
    # 'value']" even though the text itself ("variable_1") was fine.
    wrong_kind = {
        **VALID_BUILD_RESPONSE,
        "blocks": [
            {
                "name": "Comparison Operator",
                "slots": [
                    {"kind": "dropdown", "text": "variable_1"},
                    {"kind": "dropdown", "text": "=="},
                    {"kind": "value", "text": "1"},
                ],
            }
        ],
    }
    client = FakeClient(response_dict=wrong_kind)
    result = run(run_pipeline(client, "build", "if variable_1 equals 1"))
    assert result.ok, result.errors
    block = result.answer.blocks[0]
    assert block.slots == [
        Slot(kind="value", text="variable_1"),
        Slot(kind="dropdown", text="=="),
        Slot(kind="value", text="1"),
    ]


def test_a_run_function_slot_emitted_as_value_is_repaired_to_dropdown():
    # Caught live, verbatim from a screenshot: every single `Run function`
    # in the program had its slot emitted as `{"kind": "value", "text":
    # "faster_turn_left"}` instead of `{"kind": "dropdown", ...}` -- the
    # function name itself was exactly right, just the wrong slot kind.
    # This is the mirror image of _repair_variable_slot_kind's own
    # direction (dropdown -> value) -- safe here specifically because the
    # text is a real function name, not an arbitrary invented value.
    wrong_kind = {
        **VALID_BUILD_RESPONSE,
        "blocks": [
            {"name": "Run function", "slots": [{"kind": "value", "text": "faster_turn_left"}],
             "children": [{"name": "Stop robot", "slots": []}]},
            {"name": "Call function", "slots": [{"kind": "value", "text": "cross_forward"}]},
        ],
    }
    client = FakeClient(response_dict=wrong_kind)
    result = run(run_pipeline(client, "build", "x"))
    assert result.ok, result.errors
    assert result.answer.blocks[0].slots == [Slot(kind="dropdown", text="faster_turn_left")]
    assert result.answer.blocks[1].slots == [Slot(kind="dropdown", text="cross_forward")]


def test_a_variable_slot_repair_is_applied_at_any_nesting_depth():
    wrong_kind = {
        **VALID_BUILD_RESPONSE,
        "blocks": [
            {
                "name": "if then",
                "slots": [],
                "conditions": [
                    {
                        "name": "Comparison Operator",
                        "slots": [
                            {"kind": "dropdown", "text": "count"},
                            {"kind": "dropdown", "text": "=="},
                            {"kind": "value", "text": "1"},
                        ],
                    }
                ],
                "children": [{"name": "Stop robot", "slots": []}],
            }
        ],
    }
    client = FakeClient(response_dict=wrong_kind)
    result = run(run_pipeline(client, "build", "x"))
    assert result.ok, result.errors
    cond = result.answer.blocks[0].conditions[0]
    assert cond.slots[0] == Slot(kind="value", text="count")


def test_a_real_dropdown_slot_is_not_coerced_to_value():
    # The repair is deliberately one-directional -- a slot the catalog
    # expects to be `dropdown` (a real menu choice, e.g. the comparison
    # operator itself) must never be coerced to `value` just because it
    # came back the wrong kind; that is a different, more suspicious
    # error the validator should still catch, not paper over.
    wrong_kind = {
        **VALID_BUILD_RESPONSE,
        "blocks": [
            {
                "name": "Comparison Operator",
                "slots": [
                    {"kind": "value", "text": "1"},
                    {"kind": "value", "text": "=="},  # should be dropdown
                    {"kind": "value", "text": "1"},
                ],
            }
        ],
    }
    client = FakeClient(response_dict=wrong_kind)
    result = run(run_pipeline(client, "build", "x"))
    assert not result.ok
    assert "expected slots" in result.errors[0].message


def test_a_stray_slot_on_a_slotless_block_is_stripped():
    # Caught live via a screenshot: `Forever` (Exact Text is just the bare
    # word "Forever", no placeholder at all) came back with
    # `slots: [{"kind": "value", "text": "0"}]` -- plausibly confused with
    # `Repeat time(s)`, which does take a [Value] slot.
    stray_slot = {
        **VALID_BUILD_RESPONSE,
        "blocks": [
            {
                "name": "Forever",
                "slots": [{"kind": "value", "text": "0"}],
                "children": [{"name": "Stop robot", "slots": []}],
            }
        ],
    }
    client = FakeClient(response_dict=stray_slot)
    result = run(run_pipeline(client, "build", "x"))
    assert result.ok, result.errors
    assert result.answer.blocks[0].slots == []


def test_a_slot_count_mismatch_on_a_block_with_real_slots_is_not_touched():
    # The repair is scoped to blocks with ZERO expected slots specifically
    # -- a block that legitimately takes some slots but got the wrong
    # COUNT (as opposed to zero expected) is a different, more suspicious
    # error that must still be flagged, not silently trimmed.
    wrong_count = {
        **VALID_BUILD_RESPONSE,
        "blocks": [
            {
                "name": "Stop robot",  # takes 0 slots
                "slots": [],
            },
            {
                "name": "Move robot's",  # takes 3 slots
                "slots": [{"kind": "dropdown", "text": "Back axis"}],  # only 1 given
            },
        ],
    }
    client = FakeClient(response_dict=wrong_count)
    result = run(run_pipeline(client, "build", "x"))
    assert not result.ok
    assert any("expected slots" in e.message for e in result.errors)


def test_a_stray_variable_reference_block_in_conditions_is_folded_in():
    # Caught live: asked "if count == 5", the model put the comparison's
    # `==`/`5` in one `conditions` entry and a SEPARATE `variable_1 (oval
    # reference)` block (holding "count") in another -- `if then` allows
    # exactly one condition block, so this both over-filled `conditions`
    # ("expected 1 condition slot(s) ... got 2") and left the comparison
    # itself missing its leading slot.
    stray_reference = {
        **VALID_BUILD_RESPONSE,
        "blocks": [
            {
                "name": "if then",
                "slots": [],
                "conditions": [
                    {
                        "name": "variable_1 (oval reference)",
                        "slots": [{"kind": "dropdown", "text": "count"}],
                    },
                    {
                        "name": "Comparison Operator",
                        "slots": [
                            {"kind": "dropdown", "text": "=="},
                            {"kind": "value", "text": "5"},
                        ],
                    },
                ],
                "children": [{"name": "Stop robot", "slots": []}],
            }
        ],
    }
    client = FakeClient(response_dict=stray_reference)
    result = run(run_pipeline(client, "build", "if count equals 5, stop"))
    assert result.ok, result.errors
    if_block = result.answer.blocks[0]
    assert len(if_block.conditions) == 1
    comparison = if_block.conditions[0]
    assert comparison.name.value == "Comparison Operator"
    assert comparison.slots == [
        Slot(kind="value", text="count"),
        Slot(kind="dropdown", text="=="),
        Slot(kind="value", text="5"),
    ]


def test_a_stray_variable_reference_block_is_folded_in_at_any_nesting_depth():
    stray_reference = {
        **VALID_BUILD_RESPONSE,
        "blocks": [
            {
                "name": "Forever",
                "slots": [],
                "children": [
                    {
                        "name": "if then",
                        "slots": [],
                        "conditions": [
                            {
                                "name": "variable_1 (oval reference)",
                                "slots": [{"kind": "dropdown", "text": "count"}],
                            },
                            {
                                "name": "Comparison Operator",
                                "slots": [
                                    {"kind": "dropdown", "text": "=="},
                                    {"kind": "value", "text": "5"},
                                ],
                            },
                        ],
                        "children": [{"name": "Stop robot", "slots": []}],
                    }
                ],
            }
        ],
    }
    client = FakeClient(response_dict=stray_reference)
    result = run(run_pipeline(client, "build", "x"))
    assert result.ok, result.errors
    nested_if = result.answer.blocks[0].children[0]
    assert len(nested_if.conditions) == 1
    assert nested_if.conditions[0].slots[0] == Slot(kind="value", text="count")


def test_two_real_condition_blocks_are_left_alone_not_treated_as_stray():
    # Logical And/Or legitimately has two `conditions` entries -- the
    # repair must not fire just because a list has more than one item; it
    # only triggers when one of them is specifically the variable-reference
    # block.
    two_real_conditions = {
        **VALID_BUILD_RESPONSE,
        "blocks": [
            {
                "name": "Logical And / Or",
                "slots": [{"kind": "dropdown", "text": "and"}],
                "conditions": [
                    {
                        "name": "If the IR sensor value",
                        "slots": [
                            {"kind": "dropdown", "text": "No.3"},
                            {"kind": "dropdown", "text": "<"},
                            {"kind": "value", "text": "200"},
                        ],
                    },
                    {
                        "name": "If the IR sensor value",
                        "slots": [
                            {"kind": "dropdown", "text": "No.4"},
                            {"kind": "dropdown", "text": ">"},
                            {"kind": "value", "text": "200"},
                        ],
                    },
                ],
            }
        ],
    }
    client = FakeClient(response_dict=two_real_conditions)
    result = run(run_pipeline(client, "build", "x"))
    assert result.ok, result.errors
    assert len(result.answer.blocks[0].conditions) == 2


def test_a_condition_block_misused_as_a_wrapper_is_repaired_into_if_then():
    # Caught live via a screenshot: a whole if/elif/.../else chain built
    # entirely out of "If the IR sensor value" blocks directly holding
    # `children`/`else_children`, with no `if then` wrapper anywhere --
    # every one of them failed validation ("does not wrap a block
    # sequence", "has no else branch"), and the intended chain never
    # existed as real blocks at all.
    misused = {
        **VALID_BUILD_RESPONSE,
        "blocks": [
            {
                "name": "If the IR sensor value",
                "slots": [
                    {"kind": "dropdown", "text": "No.3"},
                    {"kind": "dropdown", "text": "<"},
                    {"kind": "value", "text": "200"},
                ],
                "children": [{"name": "Stop robot", "slots": []}],
                "else_children": [{"name": "Move robot's", "slots": [
                    {"kind": "dropdown", "text": "Back axis"},
                    {"kind": "dropdown", "text": "Forward"},
                    {"kind": "value", "text": "50"},
                ]}],
            }
        ],
    }
    client = FakeClient(response_dict=misused)
    result = run(run_pipeline(client, "build", "if IR3 is under 200, stop, else move forward"))
    assert result.ok, result.errors
    block = result.answer.blocks[0]
    assert block.name.value == "if then"
    assert len(block.conditions) == 1
    assert block.conditions[0].name.value == "If the IR sensor value"
    assert block.conditions[0].children == []
    assert block.conditions[0].else_children == []
    assert block.children[0].name.value == "Stop robot"
    assert block.else_children[0].name.value == "Move robot's"


def test_a_chain_of_misused_condition_blocks_is_repaired_at_every_level():
    # The exact shape from the live screenshot: three levels deep, each
    # "If the IR sensor value" chained into the next via its own
    # (erroneous) else_children.
    def leaf(sensor, op, then_name, else_body):
        return {
            "name": "If the IR sensor value",
            "slots": [
                {"kind": "dropdown", "text": sensor},
                {"kind": "dropdown", "text": op},
                {"kind": "value", "text": "200"},
            ],
            "children": [{"name": then_name, "slots": []}],
            "else_children": else_body,
        }

    misused_chain = {
        **VALID_BUILD_RESPONSE,
        "blocks": [
            leaf("No.3", "<", "Stop robot", [
                leaf("No.3", ">", "Stop robot", [
                    leaf("No.5", "<", "Stop robot", [{"name": "Stop robot", "slots": []}]),
                ]),
            ]),
        ],
    }
    client = FakeClient(response_dict=misused_chain)
    result = run(run_pipeline(client, "build", "x"))
    assert result.ok, result.errors

    # Every level got wrapped -- the whole chain is now real `if then`
    # blocks nested via else_children, exactly what CBlock's flattening
    # (app/static/index.html) expects to find.
    level1 = result.answer.blocks[0]
    assert level1.name.value == "if then"
    level2 = level1.else_children[0]
    assert level2.name.value == "if then"
    level3 = level2.else_children[0]
    assert level3.name.value == "if then"


def test_an_ordinary_statement_block_misused_with_children_is_not_wrapped():
    # A real regression: the wrap-repair's first version matched ANY leaf
    # with condition_count==0 and no children/else of its own -- which
    # describes nearly every ordinary statement block, not just the four
    # real Sensing condition-checks. "Move robot's" given children here is
    # a genuinely different bug (a plain action block wrongly used as a
    # wrapper) and must still be flagged as such, not silently "repaired"
    # into a nonsense if/then whose condition is an action statement.
    misused_statement = {
        **VALID_BUILD_RESPONSE,
        "blocks": [{"name": "Move robot's", "slots": [], "children": [{"name": "Stop robot", "slots": []}]}],
    }
    client = FakeClient(response_dict=misused_statement)
    result = run(run_pipeline(client, "build", "x"))
    assert not result.ok
    assert result.answer.blocks[0].name.value == "Move robot's"  # untouched, not wrapped
    assert any("does not wrap a block sequence" in e.message for e in result.errors)


class FlakyClient:
    """Returns each entry in `texts` in order across successive calls --
    stands in for a provider that occasionally returns empty/unparseable
    output on one call and succeeds immediately on the next with identical
    input (observed live against OpenRouter's free tier)."""

    def __init__(self, texts: list[str]):
        self.texts = list(texts)
        self.calls = 0
        self.temperatures: list[float] = []

    async def chat(self, system, user, schema, image_base64=None, history=None, temperature=0):
        self.temperatures.append(temperature)
        text = self.texts[self.calls]
        self.calls += 1
        return ChatResult(text=text, model="flaky-model")


def test_unparseable_output_is_retried_once_and_can_still_succeed():
    # Caught live against OpenRouter: a call that returned empty/
    # unparseable text once succeeded immediately on a bare retry with
    # identical input -- a transient provider hiccup, not the model
    # trying and getting the shape wrong (which Q16 says to show, not
    # retry).
    client = FlakyClient(["", json.dumps(VALID_BUILD_RESPONSE)])
    result = run(run_pipeline(client, "build", "make it drive forward and stop"))
    assert result.ok
    assert client.calls == 2


def test_unparseable_output_on_both_attempts_still_raises():
    client = FlakyClient(["", "also not json"])
    with pytest.raises(ModelUnavailable, match="model did not return valid JSON"):
        run(run_pipeline(client, "build", "x"))
    assert client.calls == 2


def test_the_retry_bumps_temperature_so_a_deterministic_failure_is_not_just_repeated():
    # Caught live with temperature=0 everywhere: a real request had the
    # model drop the JSON schema and write a plain-language recap instead
    # -- a genuine, repeatable model behavior, not a one-off hiccup. At
    # temperature 0 the model is deterministic, so retrying with identical
    # input just reproduces the identical wrong answer -- the retry did
    # nothing for this failure mode. The second attempt must ask for a
    # different temperature, or it's not really a second chance at all.
    client = FlakyClient(["not json", json.dumps(VALID_BUILD_RESPONSE)])
    result = run(run_pipeline(client, "build", "x"))
    assert result.ok
    assert client.temperatures == [0, 0.4]


def _empty_logical_and_or_response():
    return {
        "kind": "complete",
        "voice": "x",
        "message": "x",
        "blocks": [
            {
                "name": "if then",
                "slots": [],
                "conditions": [{"name": "Logical And / Or", "slots": [{"kind": "dropdown", "text": "and"}], "conditions": []}],
                "children": [{"name": "Stop robot", "slots": []}],
            }
        ],
        "note": None,
        "image_note": None,
    }


def test_an_empty_logical_and_or_is_retried_once_a_narrow_exception_to_q16():
    # Caught live: an entire line-tracing generation emitted SIX separate
    # `Logical And / Or` blocks, every one with its two sensor-check
    # operands simply missing -- not a shape disagreement to show and move
    # on from (Q16's usual case), but required content dropped across an
    # entire program, repeatedly, in the exact same way. No repair
    # function can rebuild operands that were never emitted anywhere.
    client = FlakyClient([json.dumps(_empty_logical_and_or_response()), json.dumps(VALID_BUILD_RESPONSE)])
    result = run(run_pipeline(client, "build", "x"))
    assert result.ok
    assert client.calls == 2


def test_an_empty_logical_and_or_on_both_attempts_still_returns_the_red_boxed_answer():
    # The retry is capped at one, same budget as the unparseable-JSON
    # retry -- if the model gets it wrong twice in a row, the student
    # still sees SOMETHING (red-boxed) rather than waiting a third time.
    client = FlakyClient([json.dumps(_empty_logical_and_or_response())] * 2)
    result = run(run_pipeline(client, "build", "x"))
    assert not result.ok
    assert any("Logical And / Or" in e.block_name for e in result.errors)
    assert client.calls == 2


def _empty_forever_response():
    return {
        "kind": "complete",
        "voice": "x",
        "message": "x",
        "blocks": [{"name": "Forever", "slots": [], "conditions": [], "children": []}],
        "note": None,
        "image_note": None,
    }


def test_an_empty_wrapper_block_is_retried_too_same_narrow_exception():
    # Same family as the Logical And/Or case -- caught live via Gemini
    # confidently calling an empty `Forever` loop a finished program.
    # `children` required and simply missing is the model dropping
    # required content, not a shape disagreement to show and move past.
    client = FlakyClient([json.dumps(_empty_forever_response()), json.dumps(VALID_BUILD_RESPONSE)])
    result = run(run_pipeline(client, "build", "x"))
    assert result.ok
    assert client.calls == 2


def test_mixing_a_dropped_content_error_with_a_genuinely_different_one_does_not_retry():
    # The retry allowlist is deliberately narrow -- if EVEN ONE error in
    # the answer is a genuinely different kind of mistake (not "required
    # content missing"), Q16's normal "show it, don't retry" still wins.
    # The second block's WRONG SLOT COUNT (not a children/wrapping issue
    # at all) is what must block the retry here -- picked specifically so
    # it doesn't also happen to trip _is_wrongly_wrapping_block, which
    # would defeat the point of this test.
    mixed = _empty_forever_response()
    mixed["blocks"].append({"name": "Move robot's", "slots": []})
    client = FlakyClient([json.dumps(mixed), "unused"])
    result = run(run_pipeline(client, "build", "x"))
    assert not result.ok
    assert any("expected slots" in e.message for e in result.errors)
    assert client.calls == 1


def _leaf_wrapping_an_empty_if_then_response():
    # Caught live, verbatim from a screenshot: the model meant "if
    # junction_count == 7, then End Program" and produced the nesting
    # backwards -- `End Program` (a leaf, never allowed `children`) as the
    # OUTER block, holding an empty `if then` inside it, instead of the
    # `if then` wrapping `End Program`.
    return {
        "kind": "complete",
        "voice": "x",
        "message": "x",
        "blocks": [
            {
                "name": "Forever", "slots": [], "conditions": [],
                "children": [
                    {
                        "name": "End Program", "slots": [],
                        "children": [
                            {
                                "name": "if then", "slots": [],
                                "conditions": [{"name": "Comparison Operator", "slots": [
                                    {"kind": "value", "text": "junction_count"},
                                    {"kind": "dropdown", "text": "=="},
                                    {"kind": "value", "text": "7"},
                                ]}],
                                "children": [],
                            }
                        ],
                    }
                ],
            }
        ],
        "note": None,
        "image_note": None,
    }


def test_a_leaf_block_wrongly_wrapping_an_if_then_is_retried_too():
    # Same family, mirror image of the empty-wrapper case: "does not wrap
    # a block sequence" (29 real occurrences across 6 different block
    # names in production logs -- not a one-off) is the model inverting
    # which of two blocks should hold the other, not a shape disagreement
    # worth just showing and moving past.
    client = FlakyClient([
        json.dumps(_leaf_wrapping_an_empty_if_then_response()),
        json.dumps(VALID_BUILD_RESPONSE),
    ])
    result = run(run_pipeline(client, "build", "x"))
    assert result.ok
    assert client.calls == 2


def _self_nested_forever_response(depth=3):
    # Caught live, verbatim: a real answer nested `Forever` inside itself
    # a dozen-plus levels deep. A smaller depth here is enough to exercise
    # the same check without a giant fixture.
    block = {"name": "Stop robot", "slots": []}
    for _ in range(depth):
        block = {"name": "Forever", "slots": [], "conditions": [], "children": [block]}
    return {
        "kind": "complete", "voice": "x", "message": "x",
        "blocks": [block], "note": None, "image_note": None,
    }


def test_a_self_nested_forever_is_retried_too_same_narrow_exception():
    client = FlakyClient([
        json.dumps(_self_nested_forever_response()),
        json.dumps(VALID_BUILD_RESPONSE),
    ])
    result = run(run_pipeline(client, "build", "x"))
    assert result.ok
    assert client.calls == 2


def _if_then_missing_its_condition_response():
    return {
        "kind": "complete", "voice": "x", "message": "x",
        "blocks": [{"name": "if then", "slots": [], "conditions": [], "children": [{"name": "Stop robot", "slots": []}]}],
        "note": None, "image_note": None,
    }


def _leaf_wrongly_given_a_condition_response():
    # Caught live, verbatim: a badly garbled main-loop generation gave
    # `Add the variable by` (a plain leaf, expects 0 conditions) a
    # condition it has no slot for at all.
    return {
        "kind": "complete", "voice": "x", "message": "x",
        "blocks": [{
            "name": "Add the variable by",
            "slots": [{"kind": "dropdown", "text": "count"}, {"kind": "value", "text": "1"}],
            "conditions": [{"name": "If the IR sensor value", "slots": [
                {"kind": "dropdown", "text": "No.3"}, {"kind": "dropdown", "text": "<"}, {"kind": "value", "text": "200"},
            ]}],
        }],
        "note": None, "image_note": None,
    }


def test_a_condition_count_mismatch_is_retried_too_same_narrow_exception():
    # Caught live: the same badly-garbled main-loop generation produced
    # this on several different blocks at once, in both directions -- a
    # missing required condition, and an unexpected one where none is
    # allowed at all. Generalizes _is_empty_logical_and_or beyond just
    # that one block name.
    for broken in (_if_then_missing_its_condition_response(), _leaf_wrongly_given_a_condition_response()):
        client = FlakyClient([json.dumps(broken), json.dumps(VALID_BUILD_RESPONSE)])
        result = run(run_pipeline(client, "build", "x"))
        assert result.ok, result.errors
        assert client.calls == 2


def test_a_json_answer_wrapped_in_prose_and_a_markdown_fence_is_recovered():
    # Caught live, verbatim: "strict": True on response_format is a request
    # to the backend, not a guarantee -- an OpenRouter-routed provider
    # replied with a conversational preamble followed by the real answer
    # inside a ```json fence. The JSON itself was valid; only the wrapping
    # around it broke a bare json.loads. Recovered with ZERO extra model
    # calls -- this doesn't even need the retry attempt.
    wrapped = (
        "We'll build the main Forever loop with the full six-branch "
        "decision chain.\n\n```json\n" + json.dumps(VALID_BUILD_RESPONSE) + "\n```"
    )
    client = FlakyClient([wrapped, "unused"])
    result = run(run_pipeline(client, "build", "x"))
    assert result.ok
    assert client.calls == 1  # recovered on the first attempt, no retry needed


def test_a_bare_blocks_array_in_a_fence_is_reconstructed_into_a_real_answer():
    # Caught live, verbatim: the model wrote prose ("Here's the main
    # loop...") followed by a ```json fence holding just the raw `blocks`
    # array, not the full required object -- _extract_json_object finds
    # the fence but correctly declines it (a list isn't the expected
    # dict), so this used to still fail outright even though a real,
    # usable explanation and a real set of blocks were both right there.
    prose = "Here's the main loop that makes your robot follow the line and stop after 5 junctions."
    blocks = [{"name": "Stop robot", "slots": []}]
    wrapped = prose + "\n\nPlace it by adding a `Forever` block.\n\n```json\n" + json.dumps(blocks) + "\n```"
    client = FlakyClient([wrapped, "unused"])
    result = run(run_pipeline(client, "build", "x"))
    assert result.ok, result.errors
    assert client.calls == 1  # recovered on the first attempt, no retry needed
    assert result.answer.kind == "complete"
    assert result.answer.message.startswith(prose)
    assert result.answer.voice  # non-empty -- reused from the model's own prose
    assert len(result.answer.blocks) == 1


def test_a_bare_blocks_array_with_no_prose_at_all_is_not_reconstructed():
    # No prose prefix means there's nothing honest to put in
    # message/voice -- must decline rather than invent text from nothing.
    blocks = [{"name": "Stop robot", "slots": []}]
    wrapped = "```json\n" + json.dumps(blocks) + "\n```"
    client = FlakyClient([wrapped, "also not an object"])
    with pytest.raises(ModelUnavailable, match="model did not return valid JSON"):
        run(run_pipeline(client, "build", "x"))
    assert client.calls == 2


def test_a_bare_blocks_array_is_not_reconstructed_for_debug_mode():
    # Debug's answer shape (summary/fault, not message) doesn't have this
    # same split -- deliberately scoped to build only.
    prose = "Here's what's wrong with your program."
    blocks = [{"name": "Stop robot", "slots": []}]
    wrapped = prose + "\n\n```json\n" + json.dumps(blocks) + "\n```"
    client = FlakyClient([wrapped, "also not an object"])
    with pytest.raises(ModelUnavailable, match="model did not return valid JSON"):
        run(run_pipeline(client, "debug", "x"))
    assert client.calls == 2


def test_a_top_level_json_array_is_treated_as_unparseable_not_a_crash():
    # Caught live: the text parsed as valid JSON, but as a bare array, not
    # the expected object -- every repair function and Pydantic model
    # assumes a dict, and this used to crash straight through with a raw
    # `'list' object has no attribute 'get'` AttributeError that leaked to
    # the student as an unhelpful "something went wrong". Must fail the
    # same clean way an actually-malformed response does, not crash.
    client = FlakyClient(["[1, 2, 3]", "also not an object"])
    with pytest.raises(ModelUnavailable, match="model did not return valid JSON"):
        run(run_pipeline(client, "build", "x"))
    assert client.calls == 2


def test_pathologically_nested_json_is_treated_as_unparseable_not_a_crash():
    # Caught live: a response that was technically well-formed JSON but
    # uselessly, pathologically deep (some repetition the model got stuck
    # in, nesting the same shape thousands of levels instead of ever
    # closing it) blew Python's own recursive-descent JSON decoder's call
    # stack -- a bare `RecursionError` ("maximum recursion depth
    # exceeded...") leaked straight to the student, since it isn't a
    # json.JSONDecodeError and nothing caught it before now. Must fail the
    # same clean way an actually-malformed response does, not crash.
    pathological = "[" * 5000 + "]" * 5000
    client = FlakyClient([pathological, "also not an object"])
    with pytest.raises(ModelUnavailable, match="model did not return valid JSON"):
        run(run_pipeline(client, "build", "x"))
    assert client.calls == 2


def test_the_raw_text_preview_is_included_when_both_attempts_fail():
    # The bare JSONDecodeError ("char 0") never said what the model
    # actually sent -- markdown fences, a leading apology, and genuinely
    # empty text all fail identically otherwise, with no way to tell them
    # apart after the fact from the logged error alone.
    client = FlakyClient(["", "Here's a summary of what we built..."])
    with pytest.raises(ModelUnavailable, match="raw text preview"):
        run(run_pipeline(client, "build", "x"))


def test_a_single_valid_response_does_not_trigger_a_second_call():
    client = FlakyClient([json.dumps(VALID_BUILD_RESPONSE), "unused"])
    result = run(run_pipeline(client, "build", "x"))
    assert result.ok
    assert client.calls == 1


def test_system_prompt_targets_a_kid_not_a_colleague():
    # Feedback: the assistant's own prose read too formal for the actual
    # audience (age 7-12) -- students lost interest. "10 words or fewer"
    # and "aged 7-12" are the two concrete, checkable commitments in the
    # rewritten tone rule, not just vague "be friendly" language a model
    # could ignore just as easily as the old "plain everyday words" one.
    client = FakeClient(response_dict=VALID_BUILD_RESPONSE)
    run(run_pipeline(client, "build", "x"))
    system = client.last_call["system"]
    assert "aged 7-12" in system
    assert "10 words or fewer" in system


def test_system_prompt_shows_a_concrete_numbered_list_example():
    # Feedback: a real message came back as one dense paragraph ("First
    # we'll set up two variables and create the first two functions...")
    # instead of visibly separated steps. Abstract "break it into short
    # points" guidance wasn't concrete enough -- a real before/after
    # example with actual newlines and "1. "/"2. " markers (which
    # index.html's RichText renders as a real numbered list) is a much
    # stronger, checkable signal.
    client = FakeClient(response_dict=VALID_BUILD_RESPONSE)
    run(run_pipeline(client, "build", "x"))
    system = client.last_call["system"]
    assert system  # the wording check lives in the private test suite


def test_system_prompt_requires_a_newline_after_every_sentence_mechanically():
    # Follow-up feedback: the numbered-list rule alone wasn't enough --
    # real messages still came back dense (a plain two-sentence
    # clarifying question run together on one line), and one attempt at a
    # list cut off after "1." with nothing following it. "Judge whether
    # this qualifies as multiple things" was too much discretion for the
    # model to get right consistently -- a mechanical "newline after every
    # sentence, always" rule needs no judgment call, and finishing every
    # list you start is now explicit too.
    client = FakeClient(response_dict=VALID_BUILD_RESPONSE)
    run(run_pipeline(client, "build", "x"))
    system = client.last_call["system"]
    assert system  # the wording check lives in the private test suite
    assert system  # the wording check lives in the private test suite
    assert system  # the wording check lives in the private test suite


def test_system_prompt_forbids_bundling_the_main_loop_into_a_complete_wrapup():
    # Caught live: after variables and functions each got their own phase,
    # a third continuation bundled the ENTIRE main sensor decision chain
    # into one `kind: "complete"` turn framed as "just add the final big
    # loop" -- and produced a catastrophically garbled program (13
    # structural errors: `Forever` nested inside itself, whole `if then`
    # chains stuffed into `conditions`). The main loop must get its own
    # dedicated phase, never a rushed afterthought.
    client = FakeClient(response_dict=VALID_BUILD_RESPONSE)
    run(run_pipeline(client, "build", "x"))
    system = client.last_call["system"]
    assert system  # the wording check lives in the private test suite
    assert system  # the wording check lives in the private test suite


def test_the_numbered_list_example_also_covers_kind_complete_not_just_phase():
    # Live testing showed the model choosing kind="complete" far more
    # often than kind="phase" for these requests -- the concrete numbered
    # example needs to live wherever `message` actually gets written, not
    # just under the "phase" bullet where it happened to be added first.
    client = FakeClient(response_dict=VALID_BUILD_RESPONSE)
    run(run_pipeline(client, "build", "x"))
    system = client.last_call["system"]
    # Literal backslash-n, not a real newline -- this example shows raw
    # JSON source text (where a newline inside a string MUST be escaped
    # this way to be valid JSON at all), unlike the earlier prose example
    # which shows real content and so uses a real newline.
    assert system  # the wording check lives in the private test suite


def test_system_prompt_treats_the_student_as_a_complete_novice():
    # Follow-up feedback: an explicit banned-word list (with the plain
    # replacement spelled out for each one) was more machinery than
    # wanted -- a general instruction to reach for plain words is enough
    # guidance without hardcoding a specific vocabulary list into the
    # prompt.
    client = FakeClient(response_dict=VALID_BUILD_RESPONSE)
    run(run_pipeline(client, "build", "x"))
    system = client.last_call["system"]
    assert "COMPLETE NOVICE" in system
    assert system  # the wording check lives in the private test suite


def test_the_router_context_is_included_in_the_system_prompt():
    client = FakeClient(response_dict=VALID_BUILD_RESPONSE)
    run(run_pipeline(client, "build", "how do I turn the wheels"))
    assert "Move robot's" in client.last_call["system"]


def test_routing_considers_the_whole_conversation_not_just_the_latest_reply():
    # Observed live: a confirmation reply like "Yes, build it please." carries
    # none of the original request's keywords. Routing on it alone would
    # silently drop a skill-doc that was clearly relevant one turn earlier
    # (e.g. line-following's junction handling), and the model would fall
    # back to a naive single-sensor build.
    client = FakeClient(response_dict=VALID_BUILD_RESPONSE)
    history = [
        {"role": "user", "content": "make my robot follow the line around corners"},
        {"role": "assistant", "content": "Here's the plan. Does this match what you want?"},
    ]
    skill = SkillDoc(id="line-following", name="Line Following", triggers=["line"], intro="junction handling")
    run(run_pipeline(client, "build", "Yes, build it please.", history=history, skills=[skill]))
    assert "junction" in client.last_call["system"].lower()


def test_no_skills_argument_means_no_retrieval_and_no_injection():
    # Default behaviour when a caller doesn't opt in (skills=None) -- must
    # stay a pure no-op, not attempt any retrieval, so this module is free
    # of network calls unless a caller deliberately wires a real store and
    # embedder in (app/api/main.py's real /ask path does).
    client = FakeClient(response_dict=VALID_BUILD_RESPONSE)
    run(run_pipeline(client, "build", "make my robot follow the line around corners"))
    assert "Relevant strategy pattern" not in client.last_call["system"]


def test_an_empty_skills_list_is_also_a_no_op():
    client = FakeClient(response_dict=VALID_BUILD_RESPONSE)
    run(run_pipeline(client, "build", "make my robot follow the line around corners", skills=[]))
    assert "Relevant strategy pattern" not in client.last_call["system"]


def test_a_matched_skill_is_rendered_into_the_system_prompt():
    client = FakeClient(response_dict=VALID_BUILD_RESPONSE)
    skill = SkillDoc(id="line-following", name="Line Following", triggers=["line"], intro="Use multiple sensors.")
    run(run_pipeline(client, "build", "make my robot follow the line", skills=[skill]))
    assert "Relevant strategy pattern" in client.last_call["system"]
    assert "Use multiple sensors." in client.last_call["system"]


def test_an_unmatched_question_does_not_inject_an_irrelevant_skill():
    client = FakeClient(response_dict=VALID_BUILD_RESPONSE)
    skill = SkillDoc(id="line-following", name="Line Following", triggers=["line"], intro="Use multiple sensors.")
    run(run_pipeline(client, "build", "make it beep three times", skills=[skill]))
    assert "Relevant strategy pattern" not in client.last_call["system"]


def test_block_names_stay_english_instruction_is_present_for_every_language():
    client = FakeClient(response_dict=VALID_BUILD_RESPONSE)
    run(run_pipeline(client, "build", "turn left", language="Bahasa Malaysia"))
    assert "ALWAYS stay in English" in client.last_call["system"]
    assert "Bahasa Malaysia" in client.last_call["system"]


def test_build_extras_own_language_placeholder_is_actually_substituted():
    # Caught live: _BUILD_EXTRA's `{language}` is inserted as a raw
    # substituted VALUE into _SYSTEM_TEMPLATE's own .format() call, which
    # does not recursively re-scan that value for further `{...}`
    # placeholders -- the literal text "{language}" was leaking straight
    # into the real prompt sent to the model instead of "Bahasa Malaysia".
    client = FakeClient(response_dict=VALID_BUILD_RESPONSE)
    run(run_pipeline(client, "build", "x", language="Bahasa Malaysia"))
    system = client.last_call["system"]
    assert "{language}" not in system
    assert system  # the wording check lives in the private test suite


def test_build_extras_json_examples_render_as_single_braces_not_double():
    # Same root cause as the {language} bug above: `{{"kind": "phase", ...}}`
    # was written assuming _BUILD_EXTRA would go through its own format()
    # call (hence the escaping), but it's actually inserted as a raw value
    # -- so those double braces were never collapsed to single ones, and
    # the model saw literal "{{...}}" instead of a JSON-shaped example.
    client = FakeClient(response_dict=VALID_BUILD_RESPONSE)
    run(run_pipeline(client, "build", "x"))
    system = client.last_call["system"]
    assert '{{"kind"' not in system
    assert system  # the wording check lives in the private test suite


def test_system_prompt_prefers_a_linear_chain_over_nested_ifs():
    # Live testing kept showing deeply nested if-inside-if structures for
    # what's logically one flat if/elif/elif/.../else chain -- this
    # instruction asks the model to reason about the chain linearly first,
    # then translate that into R Block's containment shape, rather than
    # inventing extra nesting.
    client = FakeClient(response_dict=VALID_BUILD_RESPONSE)
    run(run_pipeline(client, "build", "x"))
    system = client.last_call["system"]
    assert "LINEAR if/elif/elif" in system


def test_system_prompt_distinguishes_unrelated_checks_from_a_real_chain():
    # Caught live via a screenshot: a standalone, unrelated check ("stop
    # after N junctions") was chained on as the head of the 4-way
    # direction-decision elif chain, dragging unrelated logic into a deeply
    # nested else_children and reading as one long, confusing block instead
    # of two separate, independent pieces of logic.
    client = FakeClient(response_dict=VALID_BUILD_RESPONSE)
    run(run_pipeline(client, "build", "x"))
    system = client.last_call["system"]
    assert system  # the wording check lives in the private test suite
    assert system  # the wording check lives in the private test suite


def test_system_prompt_extends_the_conditions_rule_beyond_if_then():
    # Caught live via a screenshot: `Wait while` emitted with `conditions`
    # entirely empty -- the same "condition goes in `conditions`, never
    # `slots`" mistake the prompt already covered for `if then`, but on a
    # block whose condition isn't spelled out as a literal [Condition]
    # token in its own Exact Text (only described in Remarks).
    client = FakeClient(response_dict=VALID_BUILD_RESPONSE)
    run(run_pipeline(client, "build", "x"))
    system = client.last_call["system"]
    assert system  # the wording check lives in the private test suite


def test_system_prompt_discourages_nesting_unless_actually_needed():
    client = FakeClient(response_dict=VALID_BUILD_RESPONSE)
    run(run_pipeline(client, "build", "x"))
    system = client.last_call["system"]
    assert "Avoid nesting a loop or `if then` INSIDE another loop's/if's `children` \
unless the task genuinely requires it" in system
    # The elif chain itself (nested via else_children) must NOT be swept up
    # in this -- that nesting is separately taught as correct and expected.
    assert system  # the wording check lives in the private test suite


def test_system_prompt_suggests_functions_for_a_long_main_loop():
    client = FakeClient(response_dict=VALID_BUILD_RESPONSE)
    run(run_pipeline(client, "build", "x"))
    system = client.last_call["system"]
    assert system  # the wording check lives in the private test suite


def test_build_system_prompt_explains_the_three_turn_kinds():
    client = FakeClient(response_dict=VALID_BUILD_RESPONSE)
    run(run_pipeline(client, "build", "x"))
    system = client.last_call["system"]
    assert '"ask"' in system and '"phase"' in system and '"complete"' in system


def test_build_system_prompt_forbids_re_asking_for_confirmation():
    # Caught live: three turns in a row each re-asked essentially the same
    # plan-confirmation question ("please confirm...", "is this correct?",
    # "please confirm whether to start building") while the student had
    # already said "ok" twice -- never once producing an actual block.
    client = FakeClient(response_dict=VALID_BUILD_RESPONSE)
    run(run_pipeline(client, "build", "x"))
    system = client.last_call["system"]
    assert "MUST be" in system and '`kind: "phase"` or `kind: "complete"`' in system
    assert system  # the wording check lives in the private test suite


def test_build_system_prompt_forbids_an_empty_wrapper_block():
    # Caught live against Gemini: a "phase 2 of 2" turn whose whole job was
    # the main loop's logic came back as just an empty `Forever` with no
    # children -- confidently labelled "Complete program built." The
    # validator (app/schema/validate.py) now catches this, but the
    # system prompt should also tell the model not to do it in the first
    # place, since seeing the error is not the same as getting it right.
    client = FakeClient(response_dict=VALID_BUILD_RESPONSE)
    run(run_pipeline(client, "build", "x"))
    system = client.last_call["system"]
    assert "empty" in system.lower() and "Forever" in system


def test_build_system_prompt_forbids_narrating_content_instead_of_delivering_it():
    # Caught live: a kind="phase" turn whose message narrated the plan in
    # prose ("we'll set up the sensor checks and actions...") while blocks
    # held only a bare empty Forever, AND phase_number/phase_total were
    # both left null -- all three problems in the same turn.
    client = FakeClient(response_dict=VALID_BUILD_RESPONSE)
    run(run_pipeline(client, "build", "x"))
    system = client.last_call["system"]
    assert system  # the wording check lives in the private test suite


def test_build_system_prompt_skips_confirmation_for_a_fully_matched_pattern():
    # Caught live: "just line tracing" (which the "Line Following" pattern
    # answers completely and deterministically -- sensors, formula, all
    # fixed) still went through a full "is that correct?" round before
    # building, for a request with nothing left to actually confirm.
    client = FakeClient(response_dict=VALID_BUILD_RESPONSE)
    run(run_pipeline(client, "build", "x"))
    system = client.last_call["system"]
    assert system  # the wording check lives in the private test suite
    assert '"just line tracing"' in system
    # The exception is explicitly narrow -- must not silently swallow the
    # case where a real detail is genuinely still open.
    assert "This exception is narrow" in system


def test_system_prompt_forbids_a_prose_or_pseudocode_answer():
    # Caught live, repeatedly: asked to modify an already-built program,
    # the model answered in plain English or pseudocode instead of the
    # real schema JSON -- unparseable and unrecoverable no matter how
    # correct the reasoning in it was. Applies to both modes equally, so
    # checked against Build's own prompt here.
    client = FakeClient(response_dict=VALID_BUILD_RESPONSE)
    run(run_pipeline(client, "build", "add turn left at count 2"))
    system = client.last_call["system"]
    assert "nothing else, ever" in system
    assert "pseudocode" in system


def test_debug_system_prompt_says_never_make_the_student_guess():
    # Reversed from "Debug never asks anything" (grilling session,
    # feedback that a too-vague report left a student stuck with no way
    # forward) -- what does NOT change is Debug never turning a KNOWN
    # fault into a guessing game.
    client = FakeClient(
        response_dict={
            "kind": "answer",
            "voice": "x",
            "summary": "Fixed the loop.",
            "fault": "x",
            "blocks": [{"name": "Stop robot", "slots": []}],
            "note": None,
            "image_note": None,
        }
    )
    run(run_pipeline(client, "debug", "x"))
    assert "guess" in client.last_call["system"].lower()


def test_debug_mode_asks_for_a_fault_field():
    client = FakeClient(
        response_dict={
            "kind": "answer",
            "voice": "x",
            "summary": "Fixed the loop.",
            "blocks": [{"name": "Stop robot", "slots": []}],
            "note": None,
            "image_note": None,
            "fault": "The sensor threshold was never reset inside the loop.",
        }
    )
    result = run(run_pipeline(client, "debug", "my loop never stops"))
    assert result.ok
    assert result.answer.fault.startswith("The sensor threshold")
    assert "fault" in client.last_call["schema"]["properties"]


def test_an_ask_turn_has_no_blocks():
    client = FakeClient(
        response_dict={
            "kind": "ask", "voice": "x", "message": "Which sensor port is it on?",
            "blocks": [], "note": None, "image_note": None,
        }
    )
    result = run(run_pipeline(client, "build", "make it follow a line"))
    assert result.ok
    assert result.answer.kind == "ask"
    assert result.answer.blocks == []


def test_a_phase_turn_carries_phase_numbers():
    client = FakeClient(
        response_dict={
            "kind": "phase",
            "voice": "x",
            "message": "Phase 1: set up the counter variable",
            "blocks": [{"name": "Stop robot", "slots": []}],
            "phase_number": 1,
            "phase_total": 3,
            "note": None,
            "image_note": None,
        }
    )
    result = run(run_pipeline(client, "build", "a long line-following program with counting"))
    assert result.ok
    assert result.answer.kind == "phase"
    assert result.answer.phase_number == 1
    assert result.answer.phase_total == 3


def test_an_ask_turn_with_blocks_is_flagged_by_validation():
    client = FakeClient(
        response_dict={
            "kind": "ask",
            "voice": "x",
            "message": "x",
            "blocks": [{"name": "Stop robot", "slots": []}],
            "note": None,
            "image_note": None,
        }
    )
    result = run(run_pipeline(client, "build", "x"))
    assert not result.ok
    assert any("kind=ask must not include blocks" in e.message for e in result.errors)


def test_history_is_forwarded_to_the_client_for_a_continuation():
    client = FakeClient(response_dict=VALID_BUILD_RESPONSE)
    prior = [{"role": "user", "content": "make it follow a line"}, {"role": "assistant", "content": "Which sensor port?"}]
    run(run_pipeline(client, "build", "No.1", history=prior))
    assert client.last_call["history"] == prior


def test_no_history_means_none_is_passed_through():
    client = FakeClient(response_dict=VALID_BUILD_RESPONSE)
    run(run_pipeline(client, "build", "a fresh question"))
    assert client.last_call["history"] is None


def test_a_structurally_wrong_block_is_flagged_not_silently_accepted():
    bad_response = {
        "kind": "complete",
        "voice": "x",
        "message": "x",
        "blocks": [{"name": "Move robot's", "slots": [], "children": [{"name": "Stop robot"}]}],
        "note": None,
        "image_note": None,
    }
    client = FakeClient(response_dict=bad_response)
    result = run(run_pipeline(client, "build", "x"))
    assert not result.ok
    assert result.answer is not None  # still returned for partial red-box display
    assert any("does not wrap a block sequence" in e.message for e in result.errors)
    # Q16: a structurally-wrong-but-valid answer is shown, not retried --
    # only the narrow "Logical And / Or" empty-conditions case (see below)
    # is a deliberate exception to that.
    assert client.calls == 1


def test_an_invented_block_name_cannot_even_parse():
    bad_response = {
        "kind": "complete",
        "message": "x",
        "blocks": [{"name": "Wait until line sensor is Black", "slots": []}],
        "note": None,
        "image_note": None,
    }
    client = FakeClient(response_dict=bad_response)
    with pytest.raises(ModelUnavailable, match="did not match the Answer Contract"):
        run(run_pipeline(client, "build", "x"))


def test_non_json_output_raises_model_unavailable():
    client = FakeClient(raw_text="I'm happy to help! Here's your program:")
    with pytest.raises(ModelUnavailable, match="valid JSON"):
        run(run_pipeline(client, "build", "x"))


def test_image_base64_is_forwarded_to_the_client():
    client = FakeClient(response_dict=VALID_BUILD_RESPONSE)
    run(run_pipeline(client, "build", "what's wrong with this", image_base64="ZmFrZQ=="))
    assert client.last_call["image_base64"] == "ZmFrZQ=="


def test_two_conditions_on_a_one_condition_block_are_wrapped_in_logical_and():
    # Caught live, four times in one answer: the model wrapped the FIRST
    # branch's two sensor checks correctly in a `Logical And / Or`, then
    # dropped both checks straight into the next four `if then` blocks'
    # own `conditions` as siblings -- "expected 1 condition slot(s) for
    # 'if then', got 2" each time.
    unwrapped = {
        **VALID_BUILD_RESPONSE,
        "blocks": [
            {
                "name": "if then",
                "slots": [],
                "conditions": [
                    {"name": "If the IR sensor value", "slots": [
                        {"kind": "dropdown", "text": "No.3"},
                        {"kind": "dropdown", "text": ">"},
                        {"kind": "value", "text": "IR_value"},
                    ]},
                    {"name": "If the IR sensor value", "slots": [
                        {"kind": "dropdown", "text": "No.4"},
                        {"kind": "dropdown", "text": "<"},
                        {"kind": "value", "text": "IR_value"},
                    ]},
                ],
                "children": [{"name": "Stop robot", "slots": []}],
            }
        ],
    }
    client = FakeClient(response_dict=unwrapped)
    result = run(run_pipeline(client, "build", "x"))
    assert result.ok, result.errors
    if_block = result.answer.blocks[0]
    assert len(if_block.conditions) == 1
    wrapper = if_block.conditions[0]
    assert wrapper.name.value == "Logical And / Or"
    assert wrapper.slots == [Slot(kind="dropdown", text="and")]
    assert [c.name.value for c in wrapper.conditions] == [
        "If the IR sensor value", "If the IR sensor value",
    ]


def test_a_correctly_wrapped_compound_condition_is_left_alone():
    # `Logical And / Or` legitimately seats two conditions -- the repair
    # must not fire on it, or it would wrap the wrapper.
    already_correct = {
        **VALID_BUILD_RESPONSE,
        "blocks": [
            {
                "name": "if then",
                "slots": [],
                "conditions": [
                    {
                        "name": "Logical And / Or",
                        "slots": [{"kind": "dropdown", "text": "and"}],
                        "conditions": [
                            {"name": "If the IR sensor value", "slots": [
                                {"kind": "dropdown", "text": "No.3"},
                                {"kind": "dropdown", "text": "<"},
                                {"kind": "value", "text": "200"},
                            ]},
                            {"name": "If the IR sensor value", "slots": [
                                {"kind": "dropdown", "text": "No.4"},
                                {"kind": "dropdown", "text": "<"},
                                {"kind": "value", "text": "200"},
                            ]},
                        ],
                    }
                ],
                "children": [{"name": "Stop robot", "slots": []}],
            }
        ],
    }
    client = FakeClient(response_dict=already_correct)
    result = run(run_pipeline(client, "build", "x"))
    assert result.ok, result.errors
    wrapper = result.answer.blocks[0].conditions[0]
    assert wrapper.name.value == "Logical And / Or"
    # Not double-wrapped -- its own two operands are still the sensor checks.
    assert [c.name.value for c in wrapper.conditions] == [
        "If the IR sensor value", "If the IR sensor value",
    ]


def test_a_single_condition_is_not_wrapped():
    single = {
        **VALID_BUILD_RESPONSE,
        "blocks": [
            {
                "name": "if then",
                "slots": [],
                "conditions": [
                    {"name": "If the IR sensor value", "slots": [
                        {"kind": "dropdown", "text": "No.3"},
                        {"kind": "dropdown", "text": "<"},
                        {"kind": "value", "text": "200"},
                    ]},
                ],
                "children": [{"name": "Stop robot", "slots": []}],
            }
        ],
    }
    client = FakeClient(response_dict=single)
    result = run(run_pipeline(client, "build", "x"))
    assert result.ok, result.errors
    assert result.answer.blocks[0].conditions[0].name.value == "If the IR sensor value"


def test_a_conditionless_if_then_used_as_a_plain_else_wrapper_is_unwrapped():
    # Caught live: DeepSeek V4 Flash, asked to build the same six-branch
    # elif chain Qwen3-30B had already been verified clean on, wrapped its
    # `else: call forward` in an EXTRA `if then` with no condition at all,
    # instead of putting `Call function` directly in `else_children` --
    # surfaced as "expected 1 condition slot(s) for 'if then', got 0" on
    # an otherwise-correct program.
    redundant_wrapper = {
        **VALID_BUILD_RESPONSE,
        "blocks": [
            {
                "name": "if then",
                "slots": [],
                "conditions": [
                    {"name": "If the IR sensor value", "slots": [
                        {"kind": "dropdown", "text": "No.3"},
                        {"kind": "dropdown", "text": "<"},
                        {"kind": "value", "text": "IR_value"},
                    ]},
                ],
                "children": [{"name": "Stop robot", "slots": []}],
                "else_children": [
                    {
                        "name": "if then",
                        "slots": [],
                        "conditions": [],
                        "children": [
                            {"name": "Call function", "slots": [{"kind": "dropdown", "text": "forward"}]},
                        ],
                    }
                ],
            }
        ],
    }
    client = FakeClient(response_dict=redundant_wrapper)
    result = run(run_pipeline(client, "build", "x"))
    assert result.ok, result.errors
    else_children = result.answer.blocks[0].else_children
    assert len(else_children) == 1
    assert else_children[0].name.value == "Call function"


def test_a_conditionless_if_then_mixed_among_other_children_is_left_alone():
    # Scoped to `else_children` specifically -- one sitting in `children`
    # alongside a sibling is genuinely ambiguous (could be a real missing
    # condition worth surfacing as an error), so the repair must not touch
    # it, and the resulting error should still surface normally.
    ambiguous = {
        **VALID_BUILD_RESPONSE,
        "blocks": [
            {
                "name": "Forever",
                "slots": [],
                "children": [
                    {"name": "Stop robot", "slots": []},
                    {
                        "name": "if then",
                        "slots": [],
                        "conditions": [],
                        "children": [{"name": "Stop robot", "slots": []}],
                    },
                ],
            }
        ],
    }
    client = FakeClient(response_dict=ambiguous)
    result = run(run_pipeline(client, "build", "x"))
    assert not result.ok
    assert "expected 1 condition slot(s)" in result.errors[0].message


def test_a_real_else_condition_is_not_unwrapped():
    # An else_children of one `if then` block that DOES have a real
    # condition is a legitimate else-if continuation, not the redundant
    # wrapper pattern -- must be left completely alone.
    real_else_if = {
        **VALID_BUILD_RESPONSE,
        "blocks": [
            {
                "name": "if then",
                "slots": [],
                "conditions": [
                    {"name": "If the IR sensor value", "slots": [
                        {"kind": "dropdown", "text": "No.3"},
                        {"kind": "dropdown", "text": "<"},
                        {"kind": "value", "text": "IR_value"},
                    ]},
                ],
                "children": [{"name": "Stop robot", "slots": []}],
                "else_children": [
                    {
                        "name": "if then",
                        "slots": [],
                        "conditions": [
                            {"name": "If the IR sensor value", "slots": [
                                {"kind": "dropdown", "text": "No.4"},
                                {"kind": "dropdown", "text": "<"},
                                {"kind": "value", "text": "IR_value"},
                            ]},
                        ],
                        "children": [{"name": "Stop robot", "slots": []}],
                    }
                ],
            }
        ],
    }
    client = FakeClient(response_dict=real_else_if)
    result = run(run_pipeline(client, "build", "x"))
    assert result.ok, result.errors
    nested = result.answer.blocks[0].else_children[0]
    assert nested.name.value == "if then"
    assert len(nested.conditions) == 1


def test_a_prompt_override_replaces_the_default_system_template():
    # An admin-saved override (app/prompts/models.py) must actually take
    # effect -- a distinctive marker string in the override, absent from
    # the real default, proves the override was used rather than silently
    # ignored.
    override = PromptOverrides(
        system_template="MARKER {mode_label} {language} {mode_extra} {directory_context}"
    )
    client = FakeClient(response_dict=VALID_BUILD_RESPONSE)
    run(run_pipeline(client, "build", "x", prompts=override))
    assert "MARKER" in client.last_call["system"]


def test_no_override_behaves_identically_to_before_this_feature_existed():
    # PromptOverrides() with everything None -- the default an admin who
    # has never touched the Prompts page gets -- must produce the exact
    # same system prompt as passing no `prompts` argument at all.
    client_a = FakeClient(response_dict=VALID_BUILD_RESPONSE)
    client_b = FakeClient(response_dict=VALID_BUILD_RESPONSE)
    run(run_pipeline(client_a, "build", "x"))
    run(run_pipeline(client_b, "build", "x", prompts=PromptOverrides()))
    assert client_a.last_call["system"] == client_b.last_call["system"]


def test_validate_prompt_override_rejects_a_missing_placeholder():
    # Caught live category: an admin's edit that deletes {directory_context}
    # formats without error but silently drops the R Block Reference from
    # every request -- must be rejected before it can ever be saved.
    broken = DEFAULT_SYSTEM_TEMPLATE.replace("{directory_context}", "")
    error = validate_prompt_override("system_template", broken)
    assert error is not None
    assert "directory_context" in error


def test_validate_prompt_override_rejects_a_stray_brace():
    # A bare `{` or `}` left in by accident breaks .format() outright --
    # must be caught here, not as a live KeyError on the next real request.
    broken = DEFAULT_SYSTEM_TEMPLATE + " {oops}"
    error = validate_prompt_override("system_template", broken)
    assert error is not None


def test_validate_prompt_override_accepts_the_real_default_unchanged():
    # The defaults themselves must always pass their own validation --
    # otherwise "reset to default" would be impossible.
    assert validate_prompt_override("system_template", DEFAULT_SYSTEM_TEMPLATE) is None
    assert validate_prompt_override("build_extra", DEFAULT_BUILD_EXTRA) is None


def test_validate_prompt_override_accepts_a_safe_rewrite():
    # A legitimate edit -- same placeholders, different wording -- must be
    # accepted, not just the untouched default.
    rewrite = "You are a helpful assistant for {mode_label} in {language}.\n{mode_extra}\n{directory_context}"
    assert validate_prompt_override("system_template", rewrite) is None
