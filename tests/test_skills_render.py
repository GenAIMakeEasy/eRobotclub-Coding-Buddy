"""render_skill turns a SkillDoc (a name plus named functions with plain
pseudocode bodies) into the prose the model reads.

The FIRST function is the main-loop entry point (inlined into `Forever`'s
children, never a real `Create a function` block) and every function after
it is a real reusable R Block function -- caught live, a model given no
signal for which was which hedged between both readings and produced a
broken program (see render.py's docstring). These tests check that
distinction is actually visible in the rendered text, not just true in our
heads.

The chain-nesting warning is fixed boilerplate, not something a teacher's
pseudocode is relied on to restate -- checked separately."""

from app.skills.models import Function, SkillDoc
from app.skills.render import render_skill


def test_name_and_intro_are_rendered():
    doc = SkillDoc(id="x", name="Test Skill", intro="Do the thing.")
    text = render_skill(doc)
    assert "# Test Skill" in text
    assert "Do the thing." in text


def test_the_first_function_is_labelled_as_the_main_loop():
    doc = SkillDoc(id="x", name="Steering", functions=[Function(name="line_trace", body="move forward")])
    text = render_skill(doc)
    assert "MAIN LOOP" in text
    assert '"line_trace"' in text
    assert "NOT a `Create a function`" in text
    assert "  move forward" in text


def test_functions_after_the_first_are_labelled_as_reusable():
    doc = SkillDoc(
        id="x", name="Steering",
        functions=[Function(name="line_trace", body="call TURN_LEFT"), Function(name="TURN_LEFT", body="rotate left")],
    )
    text = render_skill(doc)
    assert "REUSABLE FUNCTIONS" in text
    assert "function TURN_LEFT:" in text
    assert "  rotate left" in text
    # The main-loop entry itself must never be re-labelled as reusable.
    assert "function line_trace:" not in text


def test_a_single_function_gets_no_reusable_functions_heading():
    doc = SkillDoc(id="x", name="Steering", functions=[Function(name="line_trace", body="move forward")])
    text = render_skill(doc)
    assert "REUSABLE FUNCTIONS" not in text


def test_multiple_reusable_functions_are_all_rendered_in_order():
    doc = SkillDoc(
        id="x", name="Steering",
        functions=[
            Function(name="line_trace", body="call TURN_LEFT"),
            Function(name="TURN_LEFT", body="rotate left"),
            Function(name="TURN_RIGHT", body="rotate right"),
        ],
    )
    text = render_skill(doc)
    assert text.index("function TURN_LEFT:") < text.index("function TURN_RIGHT:")


def test_if_elif_else_bodies_are_indented_and_preserved_verbatim():
    doc = SkillDoc(
        id="x", name="Steering",
        functions=[Function(name="line_trace", body="if IR3 < IR:\n  turn left\nelse:\n  move forward")],
    )
    text = render_skill(doc)
    assert "  if IR3 < IR:" in text
    assert "    turn left" in text
    assert "  else:" in text
    assert "    move forward" in text


def test_chain_nesting_note_present_when_there_are_functions():
    doc = SkillDoc(id="x", name="Steering", functions=[Function(name="f", body="if a: b")])
    assert "ONE chain" in render_skill(doc)


def test_chain_nesting_note_absent_when_there_are_no_functions():
    doc = SkillDoc(id="x", name="No Functions", intro="Just an intro.")
    assert "ONE chain" not in render_skill(doc)
    assert "MAIN LOOP" not in render_skill(doc)
