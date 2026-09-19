"""Turns one student Enquiry into a validated Answer.

Wires together the pieces built so far: the router picks which tabs of the
Block Directory go into context (ADR-0002), the client calls the model
constrained to the Answer Contract's JSON Schema for the chosen mode
(ADR-0001), and the result is parsed and structurally validated
(app/schema/validate.py) before it ever reaches a student. A validation
failure is not retried (Q16) — it comes back alongside the answer so the UI
can red-box the offending block.

Build follows the vendored skill's protocol (ollo-r-block-assistant.md):
ask a focused question if the request is unclear, confirm the plan, then
build phase by phase for a long program — never dump the whole thing at
once. `history` carries that conversation forward across calls; the caller
(app/api/main.py) is responsible for assembling it from session state and
for knowing when a thread has closed (kind == "complete").

Debug asks AT MOST one clarifying question before committing to a real
diagnosis (DebugAnswer.kind) — reversed from the original single-shot-only
design after live coach feedback that a student whose report was too vague
to diagnose was simply left stuck, never asked anything. `history` carries
that one question/reply exchange forward the same way Build's does; a
`kind == "answer"` turn closes the thread, same shape as Build's
`"complete"`. What does NOT change: once the fault is actually known, Debug
still states it directly — never a second question, never making the
student guess it themselves.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Literal, Optional

from pydantic import ValidationError

from app.context.router import context_for
from app.llm.ollama_client import ChatClient, ModelUnavailable
from app.prompts.models import PromptOverrides
from app.rag.embeddings import OllamaEmbedder
from app.schema import block_directory as bd
from app.schema.answer_contract import Answer, BuildAnswer, DebugAnswer, ProgramBlock, schema_for
from app.schema.validate import BlockError, validate_answer
from app.skills.models import SkillDoc
from app.skills.render import render_skill
from app.skills.retrieve import retrieve as retrieve_skill

Mode = Literal["build", "debug"]

_SKILL_CONTEXT_PREFIX = '\n\nRelevant notes for this request:\n\n'

_SYSTEM_TEMPLATE = 'You help a student with a robot block-coding task. This is a {mode_label} request. Answer in {language}. Follow the answer format you were given, and only use blocks from the reference below.\n{mode_extra}\n\nBlock reference:\n{directory_context}'

_MODE_LABEL = {"build": "Build (write a new program)", "debug": "Debug (fix a broken program)"}

_BUILD_EXTRA = 'Guide the student a little at a time: if the goal is unclear ask one short question, otherwise build the program in small steps.'

_DEBUG_EXTRA = 'Find the fault, explain it simply, and show the corrected blocks.'

_MODE_EXTRA = {"build": _BUILD_EXTRA, "debug": _DEBUG_EXTRA}

# Public, non-underscore aliases -- app/api/main.py's /admin/prompts routes
# expose these as the "default" text an admin's override is compared
# against and can reset back to. The underscored names above stay the
# single source of truth this module itself uses; these are read-only
# views onto them for the admin surface, never mutated.
DEFAULT_SYSTEM_TEMPLATE = _SYSTEM_TEMPLATE
DEFAULT_BUILD_EXTRA = _BUILD_EXTRA
DEFAULT_DEBUG_EXTRA = _DEBUG_EXTRA


_REQUIRED_SYSTEM_TEMPLATE_PLACEHOLDERS = ("{mode_label}", "{language}", "{mode_extra}", "{directory_context}")


def validate_prompt_override(kind: str, text: str) -> Optional[str]:
    """Guards an admin's edit BEFORE it can ever reach a live request --
    this prompt text is `.format()`-substituted (app/api/main.py's
    /admin/prompts save path calls this), and a stray `{`/`}` or a deleted
    placeholder doesn't fail loudly, it silently breaks every single
    request (a KeyError inside the pipeline, or a `directory_context` that
    never actually gets included) until someone notices. Returns an error
    message, or None if the text is safe to save.

    Deliberately mechanical, not semantic: this catches "the template is
    broken", never "the instructions are bad advice" -- an admin could
    still save an override that changes the model's behavior in an unwise
    way, and that's the accepted trade-off of making this fully editable
    rather than addendum-only."""
    if kind == "system_template":
        missing = [p for p in _REQUIRED_SYSTEM_TEMPLATE_PLACEHOLDERS if p not in text]
        if missing:
            return f"missing required placeholder(s): {', '.join(missing)}"
        try:
            text.format(mode_label="x", language="x", mode_extra="x", directory_context="x")
        except (KeyError, IndexError, ValueError) as exc:
            return f"template does not format correctly: {exc}"
    else:  # build_extra / debug_extra
        try:
            text.format(language="x")
        except (KeyError, IndexError, ValueError) as exc:
            return f"template does not format correctly: {exc}"
    return None


def routing_text(question: str, history: Optional[list[dict]] = None) -> str:
    """The WHOLE conversation, not just this message — also used directly
    by app/api/main.py's economy-mode path to run RAG retrieval before
    deciding whether an LLM call is even needed. A confirmation reply like
    "Yes, build it please." carries none of the original request's
    keywords — routing on it alone silently drops context (e.g. which
    skill-doc applies) that was clearly relevant one turn ago. Observed
    live: this caused a real build to skip the multi-sensor line-following
    pattern and use only No.1/No.2 instead of No.3-No.6."""
    return " ".join([*(h["content"] for h in (history or [])), question])


def _system_prompt(
    mode: Mode,
    language: str,
    routing_text: str,
    prompts: Optional[PromptOverrides] = None,
) -> str:
    # `prompts` (an admin's saved override, app/prompts/models.py) falls
    # back through TWO layers per field -- an admin's own saved "default"
    # (via the Prompts page's "Set as default"), then this module's
    # hardcoded constant -- via PromptOverrides.effective(). An admin who
    # has never touched the Prompts page gets identical behavior to before
    # this feature existed. Already validated (validate_prompt_override,
    # above) at SAVE time, not here -- re-checking on every request would
    # be pure overhead for text that can't have changed since the last
    # save.
    mode_extra_field = "build_extra" if mode == "build" else "debug_extra"
    system_template = prompts.effective("system_template", _SYSTEM_TEMPLATE) if prompts else _SYSTEM_TEMPLATE
    mode_extra_template = prompts.effective(mode_extra_field, _MODE_EXTRA[mode]) if prompts else _MODE_EXTRA[mode]

    # `mode_extra` is formatted on its own FIRST, not inserted as a raw
    # value into `system_template.format(...)` below — caught live: a
    # single outer `.format()` call does not recursively re-scan a
    # substituted value for further `{...}` placeholders, so `_BUILD_EXTRA`'s
    # own `{language}` was leaking into the real prompt as the literal
    # text "{language}", and its `{{"kind": "phase", ...}}` JSON example
    # (escaped on the assumption it WOULD go through its own format() call)
    # was leaking as literal double braces instead of valid-looking JSON.
    mode_extra = mode_extra_template.format(language=language)
    return system_template.format(
        mode_label=_MODE_LABEL[mode],
        language=language,
        mode_extra=mode_extra,
        directory_context=context_for(routing_text),
    )


def summarize_blocks(blocks: list[ProgramBlock]) -> str:
    """A compact, flat listing of block names for conversation history — so
    a later phase knows what an earlier phase already placed, without
    re-sending the full nested JSON the model doesn't need to re-parse."""
    names: list[str] = []

    def walk(block_list: list[ProgramBlock]) -> None:
        for b in block_list:
            names.append(b.name.value)
            walk(b.conditions)
            walk(b.children)
            walk(b.else_children)

    walk(blocks)
    return ", ".join(names) if names else "(none)"


@lru_cache(maxsize=1)
def _collapsed_name_patterns() -> list[tuple[re.Pattern, "bd.Block"]]:
    """One compiled regex per catalog block with a slot, built by replacing
    each `[Dropdown]`/`[Value]` placeholder in its Exact Text with a
    capture group — used to recognize when a backend collapsed a whole
    block (name AND its slot values) into the `name` field as one literal
    string, instead of using the real catalog name with separate `slots`.

    Observed live against OpenRouter (google/gemma-4-26b-a4b-it:free, which
    otherwise handles this schema well — see openrouter_client.py's
    docstring): `name` came back as `'Run function " FORWARD "'`, the
    catalog's own Exact Text (`Run function " [Dropdown] "`) with the
    dropdown already filled in, rather than `name: "Run function"` +
    `slots: [{"kind": "dropdown", "text": "FORWARD"}]`. `strict: true` on
    `response_format` does not guarantee this can't happen — enforcement
    varies by provider (OpenRouter's own docs say as much).

    Only blocks with a real literal anchor (at least a few characters of
    fixed text once placeholders are stripped) are eligible — caught live
    in testing: `Comparison Operator`'s Exact Text is just `[Value]
    [Dropdown] [Value]`, three placeholders separated by bare spaces, so
    an unanchored `(.+?)` pattern matched almost any multi-word string at
    all (a real block name that happened to contain spaces, "Wait until
    line sensor is Black", matched five different catalog blocks). A block
    whose Exact Text is nothing but placeholders and whitespace can't be
    told apart from arbitrary text this way, so it's excluded rather than
    risk false positives."""
    patterns = []
    for block in bd.load():
        if not block.slots or len(re.sub(r"\[[^\]]+\]", "", block.exact_text).strip()) < 3:
            continue
        pieces = []
        cursor = 0
        for m in re.finditer(r"\[[^\]]+\]", block.exact_text):
            pieces.append(re.escape(block.exact_text[cursor : m.start()]))
            pieces.append(r"(.+?)")
            cursor = m.end()
        pieces.append(re.escape(block.exact_text[cursor:]))
        patterns.append((re.compile("^" + "".join(pieces) + "$"), block))
    return patterns


def _repair_collapsed_block_name(block: dict) -> None:
    name = block.get("name")
    if not isinstance(name, str) or name in bd.names():
        return
    for pattern, catalog in _collapsed_name_patterns():
        m = pattern.match(name)
        if not m or len(m.groups()) != len(catalog.slot_kinds):
            continue
        block["name"] = catalog.name
        block["slots"] = [
            {"kind": kind, "text": value.strip()} for kind, value in zip(catalog.slot_kinds, m.groups())
        ]
        return


def _repair_collapsed_block_names(payload: dict) -> None:
    """Mutates `payload["blocks"]` in place, recursively, fixing any block
    whose `name` collapsed its own slot values into one string (see
    `_collapsed_name_patterns`). A backend's structured-output enforcement
    degrading into a corrected answer, not a crash — same reasoning as
    `_truncate_oversized_strings` below."""

    def walk(block: object) -> None:
        if not isinstance(block, dict):
            return
        _repair_collapsed_block_name(block)
        for key in ("conditions", "children", "else_children"):
            for child in block.get(key) or []:
                walk(child)

    for block in payload.get("blocks") or []:
        walk(block)


def _repair_variable_slot_kind(block: dict) -> None:
    """Mutates `block["slots"]` in place: a slot the catalog expects to be
    `value` but that came back `dropdown` is coerced to `value`, text
    untouched.

    Caught live: asked to compare a variable against a literal (`if count
    == 1`), the model emitted `Comparison Operator`'s first slot as
    `{"kind": "dropdown", "text": "count"}` instead of `{"kind": "value",
    "text": "count"}` — reasonable-looking, since PICKING a variable to
    update elsewhere in the same catalog (`Add the variable [Dropdown] by
    [Value]`) really is a dropdown choice, but READING a variable's current
    value into a value slot isn't a menu selection, it's the value slot's
    normal job (Value slots take "constant numeric attributes, manual
    numbers, typed text strings, or rounded reporter variables" per the
    directory's own Slot type legend). This is validation-blocking on
    every backend, constrained decoding or not, since it's a length-
    preserving KIND mismatch, not an invented block or a missing slot.

    Deliberately one-directional (dropdown -> value only, never the
    reverse) — a value emitted where a real dropdown choice (e.g. an IR
    sensor port number, a comparison operator) was expected is a different,
    more suspicious error that repairing here would silently paper over,
    the same over-repair concern `_collapsed_name_patterns` documents for
    unanchored patterns."""
    name = block.get("name")
    catalog = bd.by_name().get(name) if isinstance(name, str) else None
    slots = block.get("slots")
    if not catalog or not isinstance(slots, list) or len(slots) != len(catalog.slot_kinds):
        return
    for slot, expected_kind in zip(slots, catalog.slot_kinds):
        if isinstance(slot, dict) and expected_kind == "value" and slot.get("kind") == "dropdown":
            slot["kind"] = "value"


# `_repair_variable_slot_kind` above is deliberately one-directional
# (dropdown -> value only) -- a `value` where a real dropdown CHOICE was
# expected is usually a more suspicious error (an invented option, a
# number where a fixed menu item belongs) that repairing blind would
# paper over. `Run function`/`Call function` are a narrow, safe exception
# to that same-direction rule: their one slot is a function NAME the
# student already defined elsewhere in the very same payload via `Create
# a function` -- not an arbitrary value, a real identifier that already
# exists, easy to tell apart from a plausible-but-wrong invention. Caught
# live: 13 real occurrences across 2 separate student requests, every
# `Run function`/`Call function` in the whole program emitted as
# `{"kind": "value", "text": "faster_turn_left"}` instead of
# `{"kind": "dropdown", ...}` -- the exact function name, just the wrong
# slot kind, not a shape the model was actually confused about.
_FUNCTION_REFERENCE_BLOCK_NAMES = frozenset({"Run function", "Call function"})


def _repair_function_reference_slot_kind(block: dict) -> None:
    """Mutates `block["slots"]` in place: `Run function`/`Call function`'s
    one slot, emitted `value` instead of the catalog's `dropdown`, is
    coerced back — text (the function name) untouched. See the comment
    above this function for why this direction is safe here specifically,
    unlike the general dropdown->value-only rule."""
    if block.get("name") not in _FUNCTION_REFERENCE_BLOCK_NAMES:
        return
    slots = block.get("slots")
    if not isinstance(slots, list) or len(slots) != 1:
        return
    slot = slots[0]
    if isinstance(slot, dict) and slot.get("kind") == "value":
        slot["kind"] = "dropdown"


def _repair_variable_slot_kinds(payload: dict) -> None:
    """Recursive walk counterpart to `_repair_collapsed_block_names`, same
    in-place-payload-before-validation shape — see
    `_repair_variable_slot_kind` for what it fixes."""

    def walk(block: object) -> None:
        if not isinstance(block, dict):
            return
        _repair_variable_slot_kind(block)
        _repair_function_reference_slot_kind(block)
        for key in ("conditions", "children", "else_children"):
            for child in block.get(key) or []:
                walk(child)

    for block in payload.get("blocks") or []:
        walk(block)


def _repair_stray_slots_on_a_slotless_block(block: dict) -> None:
    """Mutates `block["slots"]` in place: a block the catalog says takes NO
    slots at all (`slot_kinds == []` — e.g. `Forever`, `Stop robot`) but
    that came back with slot data anyway is stripped back to `[]`.

    Caught live: `Forever` emitted with `slots: [{"kind": "value", "text":
    "0"}]` — plausibly the model reaching for something like a loop
    counter or confusing it with `Repeat time(s)` (which DOES take a
    [Value] slot), but `Forever`'s Exact Text is just the bare word
    "Forever", no placeholder at all. Unlike `_repair_variable_slot_kind`
    (which fixes a slot's KIND when the right number of slots is already
    present), there is no correct interpretation of stray slot data on a
    block with no slot to hold it — dropping it is the only sound repair,
    scoped tightly to the zero-slots case specifically so it can't paper
    over an actual slot-count mismatch on a block that legitimately takes
    some slots."""
    name = block.get("name")
    catalog = bd.by_name().get(name) if isinstance(name, str) else None
    if not catalog or catalog.slot_kinds:
        return
    if block.get("slots"):
        block["slots"] = []


def _repair_stray_slots_on_slotless_blocks(payload: dict) -> None:
    """Recursive walk counterpart to `_repair_variable_slot_kinds`, same
    in-place-payload-before-validation shape — see
    `_repair_stray_slots_on_a_slotless_block` for what it fixes."""

    def walk(block: object) -> None:
        if not isinstance(block, dict):
            return
        _repair_stray_slots_on_a_slotless_block(block)
        for key in ("conditions", "children", "else_children"):
            for child in block.get(key) or []:
                walk(child)

    for block in payload.get("blocks") or []:
        walk(block)


def _repair_unwrapped_compound_condition(block: dict) -> None:
    """Mutates `block["conditions"]` in place: a block that seats exactly
    ONE condition (`if then`, `Repeat until`, ...) but was handed TWO gets
    them wrapped in the `Logical And / Or` block that was missing.

    Caught live, four times in one answer: asked for the standard
    line-following chain, the model wrapped the FIRST branch's two sensor
    checks correctly (`Logical And / Or` holding both operands) and then,
    for the next four branches, dropped both checks straight into `if
    then`'s own `conditions` as siblings -- "expected 1 condition slot(s)
    for 'if then', got 2", the same mistake four times while clearly
    knowing the right shape one branch earlier.

    Defaults the joiner to "and", which is an ASSUMPTION and the reason
    this repair is scoped as tightly as it is (exactly 2 conditions on a
    block that takes exactly 1 -- never a different count, never a block
    that legitimately seats 2 like `Logical And / Or` itself). Two sensor
    thresholds tested together in one branch of a mutually-exclusive chain
    are an AND in every strategy pattern this app ships, and the
    alternative is not "get it right" but "hard-fail into a red box", so
    the assumption buys a working program rather than papering over a
    subtler error. If a genuine OR ever shows up here, this is the line
    that would need to become smarter rather than more permissive."""
    name = block.get("name")
    catalog = bd.by_name().get(name) if isinstance(name, str) else None
    if not catalog or catalog.condition_count != 1:
        return
    conditions = block.get("conditions")
    if not isinstance(conditions, list) or len(conditions) != 2:
        return
    if not all(isinstance(c, dict) for c in conditions):
        return
    block["conditions"] = [
        {
            "name": "Logical And / Or",
            "slots": [{"kind": "dropdown", "text": "and"}],
            "conditions": conditions,
        }
    ]


def _repair_unwrapped_compound_conditions(payload: dict) -> None:
    """Recursive walk counterpart to `_repair_stray_slots_on_slotless_blocks`,
    same in-place-payload-before-validation shape — see
    `_repair_unwrapped_compound_condition` for what it fixes."""

    def walk(block: object) -> None:
        if not isinstance(block, dict):
            return
        _repair_unwrapped_compound_condition(block)
        for key in ("conditions", "children", "else_children"):
            for child in block.get(key) or []:
                walk(child)

    for block in payload.get("blocks") or []:
        walk(block)


# The one catalog block whose entire purpose is reading a variable's
# current value (Exact Text `[Dropdown]`, one slot) — see
# `_repair_stray_variable_reference_block`'s docstring for why it shows up
# where it shouldn't despite the system prompt now explicitly forbidding it.
_VARIABLE_REFERENCE_BLOCK_NAME = "variable_1 (oval reference)"


def _repair_stray_variable_reference_block(block: dict) -> None:
    """Mutates `block["conditions"]` in place: a `variable_1 (oval
    reference)` block sitting alongside the real comparison is folded into
    that comparison's own leading `slots` entry and removed, instead of
    being left as a second, illegal `conditions` item.

    Caught live: asked to compare a variable against a literal ("if count
    == 5"), the model put the comparison's `==`/`5` in one `conditions`
    entry and a SEPARATE `variable_1 (oval reference)` block (holding
    "count") in another — `if then` allows exactly one condition block, so
    this both over-filled `conditions` (validate.py: "expected 1 condition
    slot(s) ... got 2") and left the comparison itself short a slot
    ("wraps a block sequence but has none" was a knock-on symptom of the
    same root cause elsewhere in that same answer). This is the same
    underlying confusion `_repair_variable_slot_kind` fixes -- the catalog
    genuinely documents this block as the "correct" way to read a
    variable, but the Answer Contract has no field to nest a whole block
    inside another block's value slot, so the ONLY valid representation is
    the flat "variable name as slot text" the system prompt teaches. A
    model reaching for the catalog's own dedicated block instead is a
    reasonable mistake, not a wild one, and worth repairing rather than
    just rejecting."""
    conditions = block.get("conditions")
    if not isinstance(conditions, list) or len(conditions) < 2:
        return
    ref_index = next(
        (i for i, c in enumerate(conditions) if isinstance(c, dict) and c.get("name") == _VARIABLE_REFERENCE_BLOCK_NAME),
        None,
    )
    if ref_index is None:
        return
    ref_slots = conditions[ref_index].get("slots") or []
    if not ref_slots or not isinstance(ref_slots[0], dict):
        return
    var_name = ref_slots[0].get("text")
    if not var_name:
        return

    sibling = next((c for i, c in enumerate(conditions) if i != ref_index and isinstance(c, dict)), None)
    if sibling is None:
        return
    sibling.setdefault("slots", [])
    if not isinstance(sibling["slots"], list):
        return
    sibling["slots"].insert(0, {"kind": "value", "text": var_name})
    conditions.pop(ref_index)


def _repair_stray_variable_reference_blocks(payload: dict) -> None:
    """Recursive walk counterpart to `_repair_variable_slot_kinds`, same
    in-place-payload-before-validation shape."""

    def walk(block: object) -> None:
        if not isinstance(block, dict):
            return
        _repair_stray_variable_reference_block(block)
        for key in ("conditions", "children", "else_children"):
            for child in block.get(key) or []:
                walk(child)

    for block in payload.get("blocks") or []:
        walk(block)


def _wrap_condition_used_as_if_then(block: dict) -> Optional[dict]:
    """If `block` is a leaf condition catalog block (no condition slot of
    its own, not a wrapper, no else per the real catalog -- e.g. "If the IR
    sensor value") but carries `children` and/or `else_children` anyway,
    it's being used AS the if/then control-flow wrapper instead of being
    seated inside one. Returns a real `if then` wrapper with this block
    (its own erroneous `children`/`else_children`/`conditions` stripped)
    as the sole condition, or None if `block` doesn't match that pattern.

    Caught live: a whole if/elif/.../else chain built entirely out of
    "If the IR sensor value" blocks directly holding `children`/
    `else_children`, with no `if then` wrapper anywhere in the answer at
    all -- validation correctly flagged every one of them ("does not wrap
    a block sequence", "has no else branch"), but the deeper problem was
    that the intended chain never existed as real blocks to begin with.
    This is the structural-repair counterpart to the system prompt bullet
    added alongside it: a block whose own name happens to start with "If"
    is not the if/then wrapper, and never gets `children` of its own.

    Scoped to blocks named literally "If ..." (the catalog's own naming
    convention for its four Sensing condition-checks: "If the IR sensor
    value", "If detected sound count", "If the remote signal is", "If the
    start button is pressed") -- NOT any leaf with condition_count==0 and
    no children/else of its own, which describes nearly every ordinary
    statement block in the whole catalog ("Move robot's", "Stop robot",
    ...). An earlier, broader version of this check wrongly "repaired" a
    genuinely-misused "Move robot's" (a real, different bug -- a plain
    action block wrongly given children) into a nonsense if/then whose
    condition was an action statement, masking that bug as if it were this
    one. See `_collapsed_name_patterns`' own docstring for the same
    over-repair lesson learned earlier this project."""
    name = block.get("name")
    if not isinstance(name, str) or not name.startswith("If "):
        return None
    catalog = bd.by_name().get(name)
    if not catalog or catalog.condition_count or catalog.has_children or catalog.has_else:
        return None
    children = block.get("children")
    else_children = block.get("else_children")
    if not children and not else_children:
        return None

    block.pop("children", None)
    block.pop("else_children", None)
    block.pop("conditions", None)  # any nested condition here is equally spurious -- no slot for it
    return {
        "name": "if then",
        "slots": [],
        "conditions": [block],
        "children": children or [],
        "else_children": else_children or [],
    }


def _repair_conditions_used_as_wrappers(payload: dict) -> None:
    """List-based walk (not the dict-only shape the other repairs use)
    because fixing this requires REPLACING an item in place, not just
    mutating its fields -- see `_wrap_condition_used_as_if_then`."""

    def walk_list(blocks: object) -> None:
        if not isinstance(blocks, list):
            return
        for i, block in enumerate(blocks):
            if not isinstance(block, dict):
                continue
            wrapped = _wrap_condition_used_as_if_then(block)
            if wrapped is not None:
                blocks[i] = wrapped
                block = wrapped
            for key in ("conditions", "children", "else_children"):
                walk_list(block.get(key))

    walk_list(payload.get("blocks"))


def _unwrap_redundant_conditionless_if_then(else_children: list) -> Optional[list]:
    """If `else_children` is exactly one `if then` block with an EMPTY
    `conditions` list, that block is a spurious wrapper around what should
    just be the else branch's own contents directly -- an unconditional
    "if" gates nothing an else branch doesn't already gate on its own.
    Returns the wrapper's own `children` + `else_children` spliced
    together (both would run unconditionally once the outer else fires),
    or None if `else_children` doesn't match that shape.

    Caught live: DeepSeek V4 Flash (app/config.py's OPENROUTER_MODEL),
    asked to build the same six-branch elif chain Qwen3-30B had already
    been verified clean on, wrapped its final `else: call forward` (and a
    nested `else: call cross_forward`) in an EXTRA `if then` with no
    condition at all, instead of just placing `Call function` directly in
    `else_children` -- surfaced to a student as "expected 1 condition
    slot(s) for 'if then', got 0" on an otherwise-correct program.

    Scoped to `else_children` specifically, not `children` generally: a
    conditionless `if then` sitting in `children` alongside other siblings
    is genuinely ambiguous -- it could just as easily be a real missing
    condition worth surfacing as an error, the same over-repair risk
    `_wrap_condition_used_as_if_then`'s own docstring already flags for a
    different pattern. One seated as the WHOLE of an else branch has no
    other plausible reading: there is nothing else that position could
    have meant besides "these blocks, unconditionally, in the else arm"."""
    if len(else_children) != 1:
        return None
    wrapper = else_children[0]
    if not isinstance(wrapper, dict) or wrapper.get("name") != "if then":
        return None
    if wrapper.get("conditions"):  # a real condition here means this ISN'T redundant
        return None
    return list(wrapper.get("children") or []) + list(wrapper.get("else_children") or [])


def _repair_redundant_conditionless_if_then_wrappers(payload: dict) -> None:
    """Same list-based-walk shape as `_repair_conditions_used_as_wrappers`
    -- see `_unwrap_redundant_conditionless_if_then`'s own docstring for
    what this catches. Looped (not a single check) so a wrapper found
    immediately inside another unwrapped wrapper -- not seen live, but
    cheap to guard against -- also gets flattened, not just the outermost
    one."""

    def walk(block: dict) -> None:
        for key in ("conditions", "children"):
            for child in block.get(key) or []:
                if isinstance(child, dict):
                    walk(child)
        while True:
            unwrapped = _unwrap_redundant_conditionless_if_then(block.get("else_children") or [])
            if unwrapped is None:
                break
            block["else_children"] = unwrapped
        for child in block.get("else_children") or []:
            if isinstance(child, dict):
                walk(child)

    for block in payload.get("blocks") or []:
        if isinstance(block, dict):
            walk(block)


def _field_max_length(spec: dict) -> Optional[int]:
    """`maxLength` sits directly on a required string field's schema, but
    under `anyOf` for an `Optional[str]` field (nullable — `note`,
    `image_note`) — check both shapes."""
    if "maxLength" in spec:
        return spec["maxLength"]
    for branch in spec.get("anyOf", []):
        if "maxLength" in branch:
            return branch["maxLength"]
    return None


def _truncate_oversized_strings(payload: dict, model_cls: type) -> None:
    """A backend's own structured-output enforcement isn't always as
    strict as Ollama's true grammar-constrained decoding — observed live,
    Gemini let a `message` string through well past the schema's
    `maxLength`, which then hard-failed Pydantic validation into a
    ModelUnavailable the student just saw as "something went wrong".
    Truncating oversized strings against the SAME schema before validation
    means a backend's looser enforcement degrades to a clipped answer, not
    a crash."""
    for key, spec in model_cls.model_json_schema().get("properties", {}).items():
        limit = _field_max_length(spec)
        value = payload.get(key)
        if limit and isinstance(value, str) and len(value) > limit:
            payload[key] = value[: limit - 1].rstrip() + "…"


_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*\n(.*?)\n?```", re.DOTALL)


def _extract_json_object(text: str) -> Optional[str]:
    """Recovers a JSON object from text that isn't PURELY JSON -- caught
    live: `response_format: {"type": "json_schema", "strict": True}` is a
    request to the backend, not a guarantee every OpenRouter-routed
    provider actually honours it. A real response came back as a prose
    preamble ("We'll build the main Forever loop...") followed by the real
    answer inside a ```json ...``` fence -- the JSON was right there,
    valid, just wrapped in exactly the conversational habit `strict` is
    supposed to suppress. Tried in order: a fenced code block first (most
    specific, least likely to grab the wrong thing), then a bare
    first-'{'-to-last-'}' slice as a looser fallback. Returns None if
    neither shape is found; the caller still needs to try `json.loads` on
    whatever this returns, since finding a `{...}`-shaped slice doesn't
    guarantee it's valid JSON."""
    fence_match = _JSON_FENCE_RE.search(text)
    if fence_match:
        return fence_match.group(1)
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return text[start : end + 1]
    return None


def _reconstruct_build_answer_from_bare_blocks_array(text: str) -> Optional[dict]:
    """Last-resort recovery, BUILD mode only: the model wrote a prose
    explanation followed by a ```json fence containing just the raw
    `blocks` array -- not the full required answer object
    (kind/voice/message/blocks). Caught live, specifically at the hardest
    generation point this app has (the main decision-chain loop -- see
    `_is_self_nested_forever`/`_is_condition_count_mismatch` for other
    failure shapes at that SAME generation point): `_extract_json_object`
    correctly finds the fence but declines to use it, since a bare list
    isn't the expected object shape -- this reconstructs a real answer
    around it instead of giving up, using the model's own prose (written
    before the fence) as the message/voice text. That prose is usually a
    perfectly reasonable summary on its own, just written in the wrong
    place structurally -- reusing it beats discarding a genuinely good
    explanation just because it arrived attached to a malformed envelope.

    Deliberately narrow: BUILD mode only (Debug's answer shape doesn't
    have this same message+blocks split), and only when there's real
    prose to reuse -- an empty prose prefix means there's nothing honest
    to put in message/voice, so this declines rather than inventing text
    from nothing. Always reconstructs as `kind: "complete"` -- the safest
    choice, since guessing `"phase"` would also require guessing
    phase_number/phase_total with no real basis for either."""
    match = _JSON_FENCE_RE.search(text)
    if not match:
        return None
    try:
        candidate = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    if not isinstance(candidate, list) or not candidate:
        return None
    prose = text[: match.start()].strip()
    if not prose:
        return None
    first_sentence = re.split(r"(?<=[.!?])\s", prose, maxsplit=1)[0].strip()
    voice = (first_sentence or prose)[:140]
    return {
        "kind": "complete",
        "voice": voice,
        "message": prose,
        "phase_number": None,
        "phase_total": None,
        "blocks": candidate,
        "note": None,
        "image_note": None,
    }


def _is_empty_logical_and_or(error: BlockError) -> bool:
    """True for validate.py's specific "Logical And / Or" got its `and`/`or`
    dropdown but zero of its two required sensor-check operands -- see
    run_pipeline's own comment on why this ONE validation-error shape gets
    a real retry (Q16's usual "show it, don't retry" default doesn't apply
    here: the two operands are gone from the payload entirely, not just in
    the wrong shape, so no repair function can rebuild them)."""
    return error.block_name == "Logical And / Or" and "got 0" in error.message


def _is_empty_wrapper_block(error: BlockError) -> bool:
    """True for validate.py's "wraps a block sequence but has none" --
    ANY wrapper block (Forever, Repeat, if then, ...) whose catalog entry
    requires `children` but got none. Same family as
    `_is_empty_logical_and_or`: content the model was required to fill in
    is simply absent from the payload, not wrong-shaped -- no repair
    function can invent a loop or branch body out of nothing, and an
    empty wrapper is never a real program (see validate.py's own comment
    on this check -- caught live via Gemini confidently calling an empty
    `Forever` loop a finished answer)."""
    return "wraps a block sequence but has none" in error.message


def _is_wrongly_wrapping_block(error: BlockError) -> bool:
    """True for validate.py's "does not wrap a block sequence" -- a leaf
    block (never allowed `children` at all -- `End Program`, a sensor
    check, `Logical And / Or`, ...) that got them anyway. Same family as
    `_is_empty_wrapper_block`, the mirror image of it: caught live, the
    model meant "if junction_count == 7, then End Program" and produced
    the nesting backwards -- `End Program` as the outer wrapper holding an
    EMPTY `if then` inside it, instead of the `if then` wrapping `End
    Program`. 29 real occurrences across 6 different block names in
    production logs, not a one-off -- the model inverting which block
    should hold which is a recurring mistake, not incidental noise."""
    return "does not wrap a block sequence" in error.message


def _is_self_nested_forever(error: BlockError) -> bool:
    """True for validate.py's "'Forever' should never wrap another
    'Forever' directly" -- caught live, a real answer nested `Forever`
    inside itself a dozen-plus levels deep, structurally valid (nothing
    else catches it) but never a real program: the outer loop would
    restart before the inner one ever mattered. No repair function can
    guess which level of nesting was the "real" one intended -- this is
    the model getting stuck in a genuine mistake, not a reasonable-but-
    wrong shape choice, so a retry gets a real chance at a sane structure
    instead."""
    return "should never wrap another 'Forever' directly" in error.message


_CONDITION_COUNT_MISMATCH_RE = re.compile(r"expected \d+ condition slot\(s\) for '.+', got \d+")


def _is_condition_count_mismatch(error: BlockError) -> bool:
    """True for ANY "expected N condition slot(s) ..., got M" mismatch,
    not just `_is_empty_logical_and_or`'s specific Logical-And/Or-got-0
    case. Caught live: the same badly-garbled main-loop generation
    produced this on several different blocks at once -- `if then`
    missing its one required condition entirely, `Add the variable by`
    given a condition it has no slot for at all (0 expected, got 1). Both
    directions (too few, too many) are equally unrecoverable by repair --
    there's no way to guess what a missing condition should have checked,
    and no safe way to decide which of several unexpected ones to
    discard. Generalizes the older, narrower check rather than replacing
    it -- kept separately for its own historical record of the case that
    motivated the retry mechanism in the first place."""
    return bool(_CONDITION_COUNT_MISMATCH_RE.search(error.message))


def _is_dropped_required_content(error: BlockError) -> bool:
    """Union of every known-unrecoverable "the model was required to fill
    this in and simply didn't" validation-error shape -- see each specific
    check's own docstring for the live case that motivated it. Deliberately
    narrow: this is the retry-eligible allowlist, not a general exception
    to Q16."""
    return (
        _is_empty_logical_and_or(error)
        or _is_empty_wrapper_block(error)
        or _is_wrongly_wrapping_block(error)
        or _is_self_nested_forever(error)
        or _is_condition_count_mismatch(error)
    )


@dataclass
class PipelineResult:
    answer: Optional[Answer]
    errors: list[BlockError]
    raw_text: str
    model: str
    # Carried straight through from ChatResult (see ollama_client.py) so the
    # API layer can tell a student their turn was quietly served by a
    # backup model, rather than that fact only ever showing up in the
    # server's own logs.
    used_fallback: bool = False
    fallback_reason: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.answer is not None and not self.errors


async def answer(
    client: ChatClient,
    mode: Mode,
    question: str,
    language: str = "English",
    image_base64: Optional[str] = None,
    history: Optional[list[dict]] = None,
    skills: Optional[list[SkillDoc]] = None,
    embedder: Optional[OllamaEmbedder] = None,
    prompts: Optional[PromptOverrides] = None,
) -> PipelineResult:
    routing = routing_text(question, history)
    system = _system_prompt(mode, language, routing, prompts)

    # Opt-in, not defaulted to a real store/embedder here — matches this
    # module's existing dependency style (ChatClient is also passed in, not
    # constructed). The real /ask path (app/api/main.py) wires in the
    # teacher-maintained skill library and a live embedder; tests generally
    # don't, which keeps them free of network calls by default.
    if skills:
        matched = await retrieve_skill(routing, skills, embedder)
        if matched:
            system += _SKILL_CONTEXT_PREFIX + render_skill(matched)

    schema = schema_for(mode)

    # One retry, ONLY for unparseable/empty output — not a general retry
    # policy (Q16 is explicit that a structurally-wrong-but-valid answer is
    # shown to the student, not retried, since a retry under a serialised
    # queue costs wall-clock time exactly when the class is busiest). This
    # is different in kind: caught live against OpenRouter's free tier
    # (which routes to rotating third-party providers), a call that
    # produced empty/unparseable text once succeeded immediately on a bare
    # retry with identical input — a transient provider hiccup, not the
    # model trying and getting the shape wrong.
    #
    # Caught live again, this time with `temperature=0` everywhere: a real
    # request had the model drop the JSON schema entirely and write a
    # plain-language "recap" instead (a genuine, repeatable behavior at the
    # "build looks finished" moment, not a one-off provider hiccup). With
    # temperature 0 the model is deterministic, so retrying with the exact
    # same input just reproduces the exact same wrong answer -- the retry
    # above did nothing for this failure mode. Attempt 2 nudges temperature
    # up specifically so a repeat of the SAME mistake isn't guaranteed.
    for attempt in range(2):
        result = await client.chat(
            system=system,
            user=question,
            schema=schema,
            image_base64=image_base64,
            history=history,
            temperature=0 if attempt == 0 else 0.4,
        )
        try:
            payload = json.loads(result.text)
            if not isinstance(payload, dict):
                # Caught live: the text parsed as valid JSON, but as a bare
                # top-level array, not the expected object -- every repair
                # function and Pydantic model below assumes a dict, and
                # crashed with a raw `'list' object has no attribute 'get'`
                # that leaked straight to the student as "something went
                # wrong" with no useful detail. Treated the same as an
                # unparseable response: retry-eligible, same diagnostic
                # preview on final failure, never a bare Python traceback
                # surfacing through the API.
                raise json.JSONDecodeError(f"parsed as {type(payload).__name__}, not an object", result.text, 0)
        except (json.JSONDecodeError, RecursionError) as exc:
            # Caught live: a response that was technically well-formed
            # JSON but pathologically, uselessly deep (the model stuck in
            # some repetition, nesting the same shape thousands of levels
            # instead of ever closing it) blew Python's recursive-descent
            # JSON decoder's own call stack -- `RecursionError`, a bare
            # "maximum recursion depth exceeded" leaking straight to the
            # student with none of the handling below (repair, retry,
            # diagnostic preview) ever running, since it isn't a
            # JSONDecodeError and this except clause didn't catch it
            # before now. Same treatment as any other unparseable
            # response -- there is no real program hiding in a JSON
            # structure a thousand levels deep either way.
            #
            # Before burning the one retry: the JSON may just be WRAPPED
            # (a prose preamble, a markdown fence) rather than actually
            # malformed -- see _extract_json_object's docstring for the
            # real live case this recovers. Cheap to try, no extra call.
            extracted = _extract_json_object(result.text)
            recovered = False
            if extracted is not None:
                try:
                    candidate = json.loads(extracted)
                    if isinstance(candidate, dict):
                        payload = candidate
                        recovered = True
                except (json.JSONDecodeError, RecursionError):
                    pass
            if not recovered and mode == "build":
                # See _reconstruct_build_answer_from_bare_blocks_array's
                # own docstring -- the fence held just the `blocks` array,
                # not the full object; reuse the model's own prose instead
                # of discarding a genuinely good explanation just because
                # it arrived attached to a malformed envelope.
                reconstructed = _reconstruct_build_answer_from_bare_blocks_array(result.text)
                if reconstructed is not None:
                    payload = reconstructed
                    recovered = True
            if not recovered:
                if attempt == 1:
                    # Diagnostic only: the parse error itself ("char 0")
                    # never said what the model actually sent -- markdown
                    # code fences around the JSON, a leading apology, or
                    # genuinely empty text all fail identically otherwise,
                    # and this run of errors had no way to tell them apart
                    # after the fact.
                    preview = repr(result.text[:200]) if result.text else "<empty>"
                    raise ModelUnavailable(
                        f"model did not return valid JSON: {exc} -- raw text preview: {preview}"
                    ) from exc
                continue  # attempt 0 only -- try again with attempt 1

        _repair_collapsed_block_names(payload)
        _repair_conditions_used_as_wrappers(payload)
        _repair_stray_variable_reference_blocks(payload)
        _repair_variable_slot_kinds(payload)
        _repair_stray_slots_on_slotless_blocks(payload)
        _repair_unwrapped_compound_conditions(payload)
        _repair_redundant_conditionless_if_then_wrappers(payload)

        model_cls = BuildAnswer if mode == "build" else DebugAnswer
        _truncate_oversized_strings(payload, model_cls)

        try:
            parsed = model_cls.model_validate(payload)
        except ValidationError as exc:
            # The schema should make this unreachable via Ollama's
            # constrained decoding, but a misbehaving/non-conforming
            # backend must still fail safely rather than crash the
            # request.
            raise ModelUnavailable(f"model output did not match the Answer Contract: {exc}") from exc

        validation = validate_answer(parsed)
        if (
            validation.errors
            and attempt == 0
            and all(_is_dropped_required_content(e) for e in validation.errors)
        ):
            # Narrow, deliberate exception to Q16 ("a structurally-wrong-
            # but-valid answer is shown to the student, not retried") --
            # that policy is about the model making a genuine, arbitrary
            # choice it got wrong, where a retry is just as likely to be
            # wrong differently. This is different: both errors this
            # covers (see _is_dropped_required_content) are the model
            # being REQUIRED to fill something in and simply not doing
            # so -- an entire line-tracing generation once emitted SIX
            # separate `Logical And / Or` blocks with their sensor-check
            # operands missing; separately, an empty `Forever`/`if then`
            # body is never a real program either. Not a shape
            # disagreement to show and move on from -- no repair function
            # can invent content that was never emitted anywhere in the
            # payload. Retrying (bumped temperature, same as the
            # unparseable-JSON retry) is worth the wall-clock cost here
            # because the alternative is handing a student a program
            # that's red-boxed and broken outright.
            continue

        return PipelineResult(
            answer=parsed,
            errors=validation.errors,
            raw_text=result.text,
            model=result.model,
            used_fallback=result.used_fallback,
            fallback_reason=result.fallback_reason,
        )
