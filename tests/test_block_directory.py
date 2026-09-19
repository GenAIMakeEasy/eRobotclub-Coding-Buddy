"""The enum is only as trustworthy as this parse, so pin its shape hard."""

import pytest

from app.schema import block_directory as bd

EXPECTED_TABS = {
    "Control": 12,
    "Calculation": 13,
    "Variable": 4,
    "Function": 4,
    "Sensing": 12,
    "Motion": 13,
    "Screen": 2,
    "Sound": 3,
    "Low-Level Bus Matrix Registers": 1,
}


def test_every_block_is_found():
    assert len(bd.load()) == sum(EXPECTED_TABS.values()) == 64


def test_blocks_are_grouped_into_the_expected_tabs():
    counts = {tab: 0 for tab in EXPECTED_TABS}
    for block in bd.load():
        counts[block.tab] += 1
    assert counts == EXPECTED_TABS


def test_names_are_unique_so_they_can_key_the_enum():
    names = bd.names()
    assert len(set(names)) == len(names)


def test_exact_text_is_not_unique_and_so_must_not_key_the_enum():
    # Two pairs collide; this is why ADR-0001 keys the enum on name instead.
    texts = [b.exact_text for b in bd.load()]
    assert len(set(texts)) == len(texts) - 2


def test_the_slot_legend_is_not_mistaken_for_a_tab():
    assert "Slot type legend" not in bd.tabs()


def test_every_block_has_exact_text():
    assert all(b.exact_text for b in bd.load())


def test_slots_are_read_out_of_the_exact_text():
    repeat = next(b for b in bd.load() if b.name == "Repeat time(s)")
    assert repeat.exact_text == "Repeat [Value] time(s)"
    assert repeat.slots == ["[Value]"]


def test_value_slot_the_two_word_placeholder_is_still_classified_as_a_value():
    # Caught live: "Define variable as" is `Define [Dropdown] as [Value
    # Slot]` -- the one two-word placeholder in the whole directory. An
    # exact-string match on "Value" silently dropped it, so validate.py
    # rejected a correct answer (both a variable name and its value) as
    # malformed, because the catalog claimed the block took only one slot.
    define_as = next(b for b in bd.load() if b.name == "Define variable as")
    assert define_as.exact_text == "Define [Dropdown] as [Value Slot]"
    assert define_as.slot_kinds == ["dropdown", "value"]


def test_condition_count_is_two_only_for_logical_and_or():
    # Caught live: the schema originally allowed only one `condition` per
    # block, so a compound sensor check ("IR3 < 200 AND IR4 > 200") had
    # nowhere to put its second operand -- the model flattened both nested
    # blocks' slots onto "Logical And / Or" itself, which then failed
    # validation as a slot-count mismatch. "Logical And / Or" is the one
    # block in the whole directory that legitimately needs two.
    and_or = next(b for b in bd.load() if b.name == "Logical And / Or")
    assert and_or.condition_count == 2

    if_then = next(b for b in bd.load() if b.name == "if then")
    assert if_then.condition_count == 1

    # "Repeat until" has no [Condition] token in Exact Text at all -- its
    # slot is only described in Remarks -- so condition_count must still
    # fall back to 1, not 0.
    repeat_until = next(b for b in bd.load() if b.name == "Repeat until")
    assert repeat_until.condition_count == 1

    stop_robot = next(b for b in bd.load() if b.name == "Stop robot")
    assert stop_robot.condition_count == 0


def test_a_changed_directory_is_refused(tmp_path):
    tampered = tmp_path / "r-block-directory.md"
    tampered.write_text("## Control\n\n### Fake\n* **Exact Text:** `Fake`\n")
    with pytest.raises(RuntimeError, match="has changed since the enum was pinned"):
        bd.parse(tampered)
