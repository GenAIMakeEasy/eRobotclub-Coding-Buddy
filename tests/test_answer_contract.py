"""The Answer Contract is only as good as its enforcement: an invented block
name must be structurally impossible, and a real block used the wrong way
must be caught, not silently accepted.

Build and Debug are deliberately separate, unrelated schemas (see
BuildAnswer's docstring in answer_contract.py) — Debug stays single-shot,
Build follows the vendored skill's ask -> confirm -> phase-by-phase protocol
via a flat `kind`-discriminated shape (chosen over a Pydantic/JSON-Schema
`oneOf` union because `oneOf` support in grammar-constrained decoding
backends is inconsistent)."""

import pytest
from pydantic import ValidationError

from app.schema.answer_contract import (
    BuildAnswer,
    DebugAnswer,
    ProgramBlock,
    Slot,
    schema_for,
)
from app.schema.validate import validate_answer


def slot(kind: str, text: str) -> Slot:
    return Slot(kind=kind, text=text)


def complete(blocks, message="x"):
    return BuildAnswer(kind="complete", voice="x", message=message, blocks=blocks)


def test_invented_block_name_is_rejected_by_the_schema():
    with pytest.raises(ValidationError):
        ProgramBlock(name="Wait until line sensor is Black")


def test_a_real_simple_block_round_trips():
    block = ProgramBlock(
        name="Move robot's",
        slots=[slot("dropdown", "Back axis"), slot("dropdown", "Forward"), slot("value", "50")],
    )
    result = validate_answer(complete([block], "Drive forward."))
    assert result.ok, result.errors


def test_a_wrapper_block_with_children_is_valid():
    repeat = ProgramBlock(
        name="Repeat time(s)",
        slots=[slot("value", "10")],
        children=[
            ProgramBlock(
                name="Move robot's",
                slots=[slot("dropdown", "Back axis"), slot("dropdown", "Forward"), slot("value", "50")],
            )
        ],
    )
    assert validate_answer(complete([repeat], "Drive forward ten times.")).ok


def test_a_wrapper_block_used_as_a_leaf_is_flagged():
    fake_wrapper = ProgramBlock(
        name="Move robot's",
        slots=[slot("dropdown", "Back axis"), slot("dropdown", "Forward"), slot("value", "50")],
        children=[ProgramBlock(name="Stop robot")],
    )
    result = validate_answer(complete([fake_wrapper]))
    assert not result.ok
    assert "does not wrap a block sequence" in result.errors[0].message


def test_an_empty_wrapper_block_is_flagged():
    # Caught live against Gemini: a kind=complete answer whose only block
    # was `Forever` with an empty `children` -- no error, a confident
    # message ("Here is your line tracing program!"), and a student left
    # with a loop that does nothing at all.
    empty_forever = ProgramBlock(name="Forever")
    result = validate_answer(complete([empty_forever], "Here is your line tracing program!"))
    assert not result.ok
    assert "wraps a block sequence but has none" in result.errors[0].message


def test_a_forever_wrapping_another_forever_is_flagged():
    # Caught live: a real answer nested `Forever` inside itself a
    # dozen-plus levels deep -- structurally valid (each one really does
    # wrap a real block sequence, so nothing else catches it) but never a
    # real program, since the outer loop restarts before the inner one
    # ever matters.
    inner = ProgramBlock(name="Forever", children=[ProgramBlock(name="Stop robot")])
    outer = ProgramBlock(name="Forever", children=[inner])
    result = validate_answer(complete([outer]))
    assert not result.ok
    assert "should never wrap another 'Forever' directly" in result.errors[0].message


def test_a_forever_wrapping_something_else_is_not_flagged():
    # The check is narrowly scoped to Forever-wraps-ONLY-Forever -- a
    # Forever with real, different content inside it is exactly what a
    # normal main loop looks like and must not be flagged.
    forever = ProgramBlock(name="Forever", children=[ProgramBlock(name="Stop robot")])
    result = validate_answer(complete([forever]))
    assert result.ok


def test_if_then_requires_a_condition():
    if_block = ProgramBlock(name="if then")  # no condition given
    result = validate_answer(complete([if_block]))
    assert not result.ok
    assert "expected 1 condition slot(s)" in result.errors[0].message


def test_if_then_with_a_real_condition_and_else_branch_is_valid():
    condition = ProgramBlock(
        name="If the IR sensor value",
        slots=[slot("dropdown", "No.1"), slot("dropdown", ">"), slot("value", "200")],
    )
    if_block = ProgramBlock(
        name="if then",
        conditions=[condition],
        children=[ProgramBlock(name="Stop robot")],
        else_children=[ProgramBlock(name="Stop robot")],
    )
    assert validate_answer(complete([if_block])).ok


def test_else_branch_on_a_block_with_no_else_is_flagged():
    repeat = ProgramBlock(
        name="Forever",
        children=[ProgramBlock(name="Stop robot")],
        else_children=[ProgramBlock(name="Stop robot")],
    )
    result = validate_answer(complete([repeat]))
    assert not result.ok
    assert "no else branch" in result.errors[0].message


def test_wrong_slot_kind_is_flagged():
    block = ProgramBlock(
        name="Move robot's",
        slots=[slot("value", "Back axis"), slot("dropdown", "Forward"), slot("value", "50")],
    )
    result = validate_answer(complete([block]))
    assert not result.ok
    assert "expected slots" in result.errors[0].message


def test_message_over_the_cap_is_rejected():
    with pytest.raises(ValidationError):
        BuildAnswer(kind="complete", voice="x", message="x" * 800, blocks=[])


def test_debug_requires_kind_and_summary():
    with pytest.raises(ValidationError):
        DebugAnswer(summary="x", blocks=[])  # missing kind
    with pytest.raises(ValidationError):
        DebugAnswer(kind="answer", blocks=[])  # missing summary
    # `fault` defaults to "" at the Python level -- legitimate for
    # kind=ask (no diagnosis yet), enforced as required-with-real-content
    # for kind=answer by validate_answer, not by the Pydantic model
    # itself. See schema_for("debug")'s own comment for why the emitted
    # JSON Schema still forces the KEY present either way.
    DebugAnswer(kind="ask", voice="x", summary="What's the robot doing wrong?", blocks=[])
    DebugAnswer(kind="answer", voice="x", summary="x", blocks=[], fault="the sensor threshold was never reset")


def test_ask_kind_requires_no_blocks():
    ask = BuildAnswer(kind="ask", voice="x", message="Which sensor port is it plugged into?", blocks=[])
    assert validate_answer(ask).ok


def test_ask_kind_with_blocks_is_flagged():
    block = ProgramBlock(name="Stop robot")
    ask = BuildAnswer(kind="ask", voice="x", message="Which sensor port?", blocks=[block])
    result = validate_answer(ask)
    assert not result.ok
    assert "kind=ask must not include blocks" in result.errors[0].message


def test_phase_kind_requires_phase_numbers():
    phase = BuildAnswer(kind="phase", voice="x", message="Phase 1: set up variables", blocks=[])
    result = validate_answer(phase)
    assert not result.ok
    assert "requires phase_number and phase_total" in result.errors[0].message


def test_phase_kind_with_valid_numbers_is_ok():
    block = ProgramBlock(name="Stop robot")
    phase = BuildAnswer(
        kind="phase", voice="x", message="Phase 1: stop safely", blocks=[block], phase_number=1, phase_total=2
    )
    assert validate_answer(phase).ok


def test_phase_number_exceeding_total_is_flagged():
    phase = BuildAnswer(
        kind="phase", voice="x", message="x", blocks=[], phase_number=3, phase_total=2
    )
    result = validate_answer(phase)
    assert not result.ok
    assert "cannot exceed phase_total" in result.errors[0].message


def test_complete_with_no_blocks_is_flagged():
    # Observed live: the model chose kind=complete with an empty program and
    # a confused, off-topic message instead of correctly asking (kind=ask).
    empty = BuildAnswer(kind="complete", voice="x", message="Okay, I understand you're providing a sequence...", blocks=[])
    result = validate_answer(empty)
    assert not result.ok
    assert "should have been kind=ask" in result.errors[0].message


def test_debug_ask_kind_requires_no_blocks():
    ask = DebugAnswer(kind="ask", voice="x", summary="What's the robot doing wrong?", blocks=[])
    assert validate_answer(ask).ok


def test_debug_ask_kind_with_blocks_is_flagged():
    block = ProgramBlock(name="Stop robot")
    ask = DebugAnswer(kind="ask", voice="x", summary="What's it doing wrong?", blocks=[block])
    result = validate_answer(ask)
    assert not result.ok
    assert "kind=ask must not include blocks" in result.errors[0].message


def test_debug_answer_kind_requires_a_real_fault():
    block = ProgramBlock(name="Stop robot")
    blank_fault = DebugAnswer(kind="answer", voice="x", summary="x", blocks=[block], fault="")
    result = validate_answer(blank_fault)
    assert not result.ok
    assert "requires a real `fault`" in result.errors[0].message


def test_debug_answer_kind_requires_blocks():
    empty = DebugAnswer(kind="answer", voice="x", summary="x", fault="the threshold was never reset", blocks=[])
    result = validate_answer(empty)
    assert not result.ok
    assert "should have been kind=ask" in result.errors[0].message


def test_debug_answer_kind_with_real_content_is_fine():
    block = ProgramBlock(name="Stop robot")
    answer = DebugAnswer(kind="answer", voice="x", summary="x", fault="the threshold was never reset", blocks=[block])
    assert validate_answer(answer).ok


def test_phase_with_no_blocks_is_also_flagged():
    # Observed live, separately from the kind=complete case above: a phase
    # turn whose message confidently described real blocks ("we'll handle
    # turning left when IR3 is low...") while `blocks` was actually empty --
    # this used to pass validation outright (only kind=complete was
    # checked), so the student's screen showed the phase's text with an
    # empty block panel underneath and no error anywhere explaining why.
    empty_phase = BuildAnswer(
        kind="phase", voice="x", message="Now we'll handle turning left and right.",
        blocks=[], phase_number=2, phase_total=4,
    )
    result = validate_answer(empty_phase)
    assert not result.ok
    assert "should have been kind=ask" in result.errors[0].message


def test_complete_with_real_blocks_is_fine():
    block = ProgramBlock(name="Stop robot")
    result = validate_answer(BuildAnswer(kind="complete", voice="x", message="x", blocks=[block]))
    assert result.ok


def test_schema_for_selects_by_mode():
    assert "fault" in schema_for("debug")["properties"]
    assert "kind" in schema_for("build")["properties"]
    assert "fault" not in schema_for("build")["properties"]
    with pytest.raises(ValueError):
        schema_for("sideways")


def test_the_block_name_enum_matches_the_directory_exactly():
    from app.schema import block_directory as bd

    schema = schema_for("build")
    enum_values = schema["$defs"]["BlockName"]["enum"]
    assert sorted(enum_values) == sorted(bd.names())


def test_the_build_schema_has_no_oneof_at_the_top_level():
    # Deliberate design constraint (see BuildAnswer's docstring): a flat
    # schema, not a discriminated union, because oneOf support in
    # grammar-constrained decoding backends is inconsistent.
    schema = schema_for("build")
    assert "oneOf" not in schema
    assert "anyOf" not in schema


def test_phase_number_and_phase_total_keys_are_required_but_stay_nullable():
    # Caught live: a kind="phase" turn that built real blocks (a genuine
    # improvement over the earlier blocks-omitted failure) still came back
    # with phase_number/phase_total both missing outright. Forcing the KEYS
    # present (schema_for's own override, not the Pydantic field itself --
    # see its comment) pushes a model to actually engage with them for a
    # real phase turn, while `null` stays legal for kind=ask/complete,
    # where they're genuinely inapplicable -- unlike `blocks`, changing the
    # field itself to be non-nullable would touch ~26 existing call sites
    # and the frontend's `!= null` checks for no real gain.
    schema = schema_for("build")
    assert "phase_number" in schema["required"]
    assert "phase_total" in schema["required"]
    # Still constructible without them -- None remains a legitimate value.
    BuildAnswer(kind="ask", voice="x", message="x", blocks=[])


def test_blocks_is_a_required_field_not_just_a_default():
    # Caught live: a field with a Pydantic default is never listed in the
    # JSON Schema's `required` -- which is exactly what let a model skip
    # emitting `blocks` at all on a real phase/complete turn (not an empty
    # array present, the KEY entirely absent from the raw JSON) across a
    # whole multi-turn build. `blocks` must always be required, the same
    # way DebugAnswer's already-no-default `blocks` field already is.
    assert "blocks" in schema_for("build")["required"]
    assert "blocks" in schema_for("debug")["required"]
    with pytest.raises(ValidationError):
        BuildAnswer(kind="ask", voice="x", message="x")  # blocks omitted entirely


def test_voice_is_required_and_separate_from_message():
    # Real prompt-side attempts (numbered lines, a fixed layout) to make
    # `message`/`fault` itself read well as speech never worked reliably
    # -- a separate, ALWAYS-required field for the spoken line moves this
    # from "ask nicely and hope" to something the schema itself guarantees
    # exists, the same way kind/phase_number/blocks already do.
    assert "voice" in schema_for("build")["required"]
    assert "voice" in schema_for("debug")["required"]
    with pytest.raises(ValidationError):
        BuildAnswer(kind="ask", message="x", blocks=[])  # voice omitted entirely
    with pytest.raises(ValidationError):
        DebugAnswer(kind="ask", summary="x", blocks=[])  # voice omitted entirely


def test_voice_cannot_be_empty():
    # Unlike `fault` (legitimately "" for kind=ask), `voice` has no
    # kind-conditional exception -- every single turn gets spoken, so an
    # empty voice is always a real mistake, never legitimate content.
    with pytest.raises(ValidationError):
        BuildAnswer(kind="ask", voice="", message="x", blocks=[])
    with pytest.raises(ValidationError):
        DebugAnswer(kind="ask", voice="", summary="x", blocks=[])


def test_voice_over_the_cap_is_rejected():
    with pytest.raises(ValidationError):
        BuildAnswer(kind="complete", voice="x" * 200, message="x", blocks=[])


def test_every_program_block_field_is_required_in_the_emitted_schema():
    # One schema hole caused a whole family of live errors: every
    # ProgramBlock list field carries a Pydantic default, so Pydantic left
    # them all out of `required` -- telling the model it could simply not
    # emit them. It did, producing "expected 1 condition slot(s) for 'if
    # then', got 0", empty Forever/Run function wrappers, and missing
    # slots. Requiring the KEYS still allows a legitimately empty list.
    for mode in ("build", "debug"):
        required = schema_for(mode)["$defs"]["ProgramBlock"]["required"]
        assert set(required) == {"name", "slots", "conditions", "children", "else_children"}, mode

    # Python-side defaults are untouched -- a leaf block still constructs
    # without spelling out four empty lists.
    assert ProgramBlock(name="Stop robot").children == []
