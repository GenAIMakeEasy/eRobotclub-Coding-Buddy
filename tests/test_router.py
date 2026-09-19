"""Static keyword routing (ADR-0002): must find the right tabs for plausible
student phrasing, over-fetch on ties, and fall back to the whole directory
when nothing matches — never under-fetch by picking exactly one tab."""

from app.context.router import context_for, route
from app.schema import block_directory as bd


def test_a_line_following_question_reaches_sensing_and_motion():
    result = route("my robot won't follow the black line, it keeps driving off to one side")
    assert "Sensing" in result.tabs
    assert "Motion" in result.tabs


def test_a_pure_motion_question_does_not_pull_in_unrelated_tabs():
    result = route("how do I make the robot turn and drive forward")
    assert "Motion" in result.tabs
    assert "Sound" not in result.tabs
    assert "Screen" not in result.tabs


def test_a_variable_question_reaches_variable():
    result = route("how do I create a variable to count how many times it loops")
    assert "Variable" in result.tabs


def test_nonsense_falls_back_to_the_whole_directory():
    result = route("xkcd qwerty asdf zzz")
    assert result.fallback is True
    assert sorted(result.tabs) == sorted(bd.tabs())


def test_fallback_context_contains_every_block_name():
    ctx = context_for("xkcd qwerty asdf zzz")
    for name in bd.names():
        assert name in ctx


def test_routed_context_is_smaller_than_the_full_directory():
    ctx = context_for("how do I make the robot beep")
    assert len(ctx) < len(bd.full_text())


def test_routed_context_contains_the_relevant_blocks_real_text():
    ctx = context_for("how do I make the robot beep a note")
    assert "Play" in ctx or "note" in ctx.lower()


def test_matched_keywords_are_real_words_from_the_question():
    result = route("the IR sensor threshold seems wrong")
    assert "Sensing" in result.matched_keywords
    assert "sensor" in result.matched_keywords["Sensing"]


def test_every_tab_is_individually_reachable_by_some_keyword():
    # If a tab can never be routed to, its blocks are permanently
    # under-fetched whenever fallback doesn't trigger.
    from app.context.router import _keyword_index

    index = _keyword_index()
    for tab in bd.tabs():
        assert index.get(tab), f"{tab} has no mechanically-derived keywords at all"
