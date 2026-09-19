"""render_tree turns a validated Answer into exactly the node shape the
approved UI's block components already render (design-demos/approved-v2.html:
CBlock, Hex, Stmt, Pill, Oval). These tests check the shape, not the pixels —
that every fixed-text/placeholder token from Exact Text is interleaved with
the block's real slot values in order, that wrappers get `body`, that `if
then` gets `conds`/`elseBody`, and that a validation failure survives as a
rendered (not dropped) node carrying its error message."""

from app.schema.answer_contract import BuildAnswer, ProgramBlock, Slot
from app.schema.render import render_tree
from app.schema.validate import BlockError, validate_answer


def slot(kind: str, text: str) -> Slot:
    return Slot(kind=kind, text=text)


def test_a_simple_block_interleaves_fixed_text_and_slots_in_order():
    block = ProgramBlock(
        name="Move robot's",
        slots=[slot("dropdown", "Back axis"), slot("dropdown", "Forward"), slot("value", "50")],
    )
    tree = render_tree(BuildAnswer(kind="complete", voice="x", message="x", blocks=[block]), [])
    node = tree[0]
    assert node["cat"] == "Motion"
    assert node["p"] == [
        ["t", "Move robot's"],
        ["d", "Back axis"],
        ["d", "Forward"],
        ["t", "at speed"],
        ["v", "50"],
    ]


def test_a_wrapper_block_gets_a_body():
    repeat = ProgramBlock(
        name="Repeat time(s)",
        slots=[slot("value", "10")],
        children=[ProgramBlock(name="Stop robot")],
    )
    tree = render_tree(BuildAnswer(kind="complete", voice="x", message="x", blocks=[repeat]), [])
    node = tree[0]
    assert node["c"] is True
    assert len(node["body"]) == 1
    assert node["body"][0]["p"] == [["t", "Stop robot"]]


def test_a_non_wrapper_block_has_no_body_key():
    block = ProgramBlock(name="Stop robot")
    tree = render_tree(BuildAnswer(kind="complete", voice="x", message="x", blocks=[block]), [])
    assert "body" not in tree[0]
    assert "c" not in tree[0]


def test_if_then_carries_its_condition_and_else_branch():
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
    tree = render_tree(BuildAnswer(kind="complete", voice="x", message="x", blocks=[if_block]), [])
    node = tree[0]

    # "[Icon] if [Condition] then" -> gear, "if", cond marker, "then"
    assert node["p"][0] == ["g"]
    assert ["cond", 0] in node["p"]
    assert "then" in [p[1] for p in node["p"] if p[0] == "t"]

    assert node["conds"][0]["cat"] == "Sensing"
    assert node["conds"][0]["p"][0] == ["t", "If the"]
    assert len(node["body"]) == 1
    assert len(node["elseBody"]) == 1


def test_a_condition_with_no_inline_token_still_gets_a_cond_marker():
    # "Repeat until" is `Repeat until` in Exact Text with no [Condition]
    # token, even though its Remarks say it has a hexagonal condition slot.
    condition = ProgramBlock(name="Standalone Numeric Value")
    block = ProgramBlock(name="Repeat until", conditions=[condition])
    tree = render_tree(BuildAnswer(kind="complete", voice="x", message="x", blocks=[block]), [])
    node = tree[0]
    assert ["cond", 0] in node["p"]
    assert node["conds"][0] is not None


def test_a_block_with_no_condition_never_gets_a_stray_cond_marker():
    block = ProgramBlock(name="Stop robot")
    tree = render_tree(BuildAnswer(kind="complete", voice="x", message="x", blocks=[block]), [])
    assert "conds" not in tree[0]
    assert not any(p[0] == "cond" for p in tree[0]["p"])


def test_logical_and_or_carries_both_of_its_conditions_by_index():
    left = ProgramBlock(
        name="If the IR sensor value",
        slots=[slot("dropdown", "No.3"), slot("dropdown", "<"), slot("value", "200")],
    )
    right = ProgramBlock(
        name="If the IR sensor value",
        slots=[slot("dropdown", "No.4"), slot("dropdown", ">"), slot("value", "200")],
    )
    and_or = ProgramBlock(name="Logical And / Or", slots=[slot("dropdown", "and")], conditions=[left, right])
    tree = render_tree(BuildAnswer(kind="complete", voice="x", message="x", blocks=[and_or]), [])
    node = tree[0]

    assert ["cond", 0] in node["p"]
    assert ["cond", 1] in node["p"]
    assert len(node["conds"]) == 2
    assert node["conds"][0]["p"][1] == ["d", "No.3"]
    assert node["conds"][1]["p"][1] == ["d", "No.4"]


def test_if_then_drops_its_own_literal_if_when_the_condition_already_starts_with_if():
    # Caught live: "If the IR sensor value" (and several other condition
    # blocks) are themselves named starting with "If" -- seated in "if
    # then"'s condition slot, that reads as a visible double "if if the
    # ...", which a teacher flagged as wrong. The condition block's own
    # text is untouched (still starts with "If the..."); only the
    # wrapper's redundant "if" token is dropped.
    cond = ProgramBlock(
        name="If the IR sensor value",
        slots=[slot("dropdown", "No.3"), slot("dropdown", "<"), slot("value", "200")],
    )
    if_block = ProgramBlock(name="if then", conditions=[cond], children=[ProgramBlock(name="Stop robot")])
    tree = render_tree(BuildAnswer(kind="complete", voice="x", message="x", blocks=[if_block]), [])
    node = tree[0]

    assert node["p"] == [["g"], ["cond", 0], ["t", "then"]]
    assert node["conds"][0]["p"][0] == ["t", "If the"]  # the condition's own text is untouched


def test_if_then_keeps_its_literal_if_when_the_condition_does_not_start_with_if():
    cond = ProgramBlock(
        name="Comparison Operator",
        slots=[slot("value", "count"), slot("dropdown", "="), slot("value", "5")],
    )
    if_block = ProgramBlock(name="if then", conditions=[cond], children=[ProgramBlock(name="Stop robot")])
    tree = render_tree(BuildAnswer(kind="complete", voice="x", message="x", blocks=[if_block]), [])
    assert ["t", "if"] in tree[0]["p"]


def test_if_then_drops_its_literal_if_when_the_first_operand_of_a_nested_and_or_starts_with_if():
    # The redundancy also shows up one level deeper: "Logical And / Or"
    # itself isn't named "If ...", but its FIRST seated operand often is,
    # and that's what a reader actually sees first.
    left = ProgramBlock(
        name="If the IR sensor value",
        slots=[slot("dropdown", "No.3"), slot("dropdown", "<"), slot("value", "200")],
    )
    right = ProgramBlock(
        name="If the IR sensor value",
        slots=[slot("dropdown", "No.4"), slot("dropdown", ">"), slot("value", "200")],
    )
    and_or = ProgramBlock(name="Logical And / Or", slots=[slot("dropdown", "and")], conditions=[left, right])
    if_block = ProgramBlock(name="if then", conditions=[and_or], children=[ProgramBlock(name="Stop robot")])
    tree = render_tree(BuildAnswer(kind="complete", voice="x", message="x", blocks=[if_block]), [])
    assert ["t", "if"] not in tree[0]["p"]


def test_ids_encode_position_by_path_so_they_are_stable_and_unique():
    inner = ProgramBlock(name="Stop robot")
    outer = ProgramBlock(name="Forever", children=[inner, inner])
    tree = render_tree(BuildAnswer(kind="complete", voice="x", message="x", blocks=[outer]), [])
    ids = [tree[0]["id"], tree[0]["body"][0]["id"], tree[0]["body"][1]["id"]]
    assert ids == ["0", "0-1-0", "0-1-1"]
    assert len(set(ids)) == 3


def test_a_validation_error_is_attached_to_the_offending_node_not_dropped():
    bad = ProgramBlock(name="Move robot's", children=[ProgramBlock(name="Stop robot")])
    answer = BuildAnswer(kind="complete", voice="x", message="x", blocks=[bad])
    result = validate_answer(answer)
    assert not result.ok

    tree = render_tree(answer, result.errors)
    node = tree[0]
    assert node["bad"] is True
    assert any("does not wrap a block sequence" in m for m in node["badMessages"])
    # still fully rendered, not replaced with a placeholder
    assert node["p"][0] == ["t", "Move robot's"]


def test_a_nested_condition_error_is_attached_to_the_condition_node():
    condition = ProgramBlock(name="Stop robot")  # not a real condition-shaped block
    if_block = ProgramBlock(name="if then", conditions=[condition], children=[])
    answer = BuildAnswer(kind="complete", voice="x", message="x", blocks=[if_block])
    errors = [BlockError(path=(0, 0, 0), block_name="Stop robot", message="wrong shape for a condition")]

    tree = render_tree(answer, errors)
    assert "bad" not in tree[0]
    assert tree[0]["conds"][0]["bad"] is True


def test_too_few_slots_does_not_crash_rendering():
    block = ProgramBlock(name="Move robot's", slots=[slot("dropdown", "Back axis")])
    tree = render_tree(BuildAnswer(kind="complete", voice="x", message="x", blocks=[block]), [])
    node = tree[0]
    assert node["p"][-1] == ["v", ""]  # missing value slot renders empty, not a crash


def test_too_many_slots_are_appended_rather_than_silently_dropped():
    block = ProgramBlock(name="Stop robot", slots=[slot("value", "99")])
    tree = render_tree(BuildAnswer(kind="complete", voice="x", message="x", blocks=[block]), [])
    node = tree[0]
    assert ["v", "99"] in node["p"]
