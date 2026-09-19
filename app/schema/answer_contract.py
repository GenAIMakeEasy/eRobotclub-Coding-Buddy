"""The Answer Contract: the fixed shape every reply to a student must take.

See docs/adr/0001-answer-contract-enforced-by-constrained-decoding.md. The
shape is enforced mechanically — Ollama's `format` parameter is given
`BuildAnswer.model_json_schema()` or `DebugAnswer.model_json_schema()` — so a
reply cannot arrive with the wrong fields, and a block name outside
`block_directory.names()` cannot be emitted at all.

Fields carry the CONTAINMENT structure the approved UI design renders
(design/direction-approved.md v2): nesting is a C-block enclosing its
contents, not indented text, so `ProgramBlock.children` mirrors that directly
rather than a flat list the UI would have to re-derive structure from.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional, Union

from pydantic import BaseModel, Field

from app.schema import block_directory as bd

_BLOCK_NAME = Enum("BlockName", {name: name for name in bd.names()})


class SlotKind(str, Enum):
    dropdown = "dropdown"
    value = "value"


class Slot(BaseModel):
    """One filled-in placeholder inside a block, e.g. the ``No.1`` in
    ``If the [Dropdown] IR sensor value...``. Order matches the block's
    Exact Text left to right."""

    kind: SlotKind
    text: str = Field(max_length=40)


class ProgramBlock(BaseModel):
    """One R Block, as it will be rendered in the phone's block panel.

    ``conditions`` holds the hexagonal block(s) seated in this block's
    condition slot(s), in Exact Text order — a list because almost every
    conditioned block (``if then``, ``Repeat until``, ...) takes exactly
    one, but ``Logical And / Or`` takes two (its left and right operand).
    See ``block_directory.Block.condition_count``. ``children`` and
    ``else_children`` hold the blocks enclosed by a C-wrapper's mouth and,
    for ``if then`` only, its optional else branch.
    """

    name: _BLOCK_NAME
    slots: list[Slot] = Field(default_factory=list)
    conditions: list["ProgramBlock"] = Field(default_factory=list)
    children: list["ProgramBlock"] = Field(default_factory=list)
    else_children: list["ProgramBlock"] = Field(default_factory=list)


ProgramBlock.model_rebuild()


_IMAGE_NOTE_FIELD = Field(
    default=None,
    max_length=200,
    description=(
        "A written description of the attached photo, filled in only when "
        "this turn had one. The session stores this instead of the image "
        "itself, so a follow-up turn doesn't re-pay the encode cost."
    ),
)

# A separate schema field, not a prose-style request inside `message`/
# `summary`/`fault` -- caught live, repeatedly: asking the model to
# structure ITS OWN free text (numbered lines, a fixed layout) inside one
# string field never worked reliably, across many worded attempts, while
# strict structured output (a real field being present at all) has been
# rock-solid all along for kind/phase_number/every other actual schema
# field in this contract. Splitting the "short enough to speak aloud"
# concern into its OWN required field, instead of hoping `message` reads
# well as speech, moves this from "ask nicely and hope" to "the schema
# guarantees it exists". `message`/`summary`/`fault` remain the fuller
# on-screen text -- can be as long/structured as it needs to be now that
# it's no longer also doing double duty as the spoken line.
_VOICE_FIELD = Field(
    min_length=1,
    max_length=150,
    description=(
        "ONE short, spoken-out-loud sentence a text-to-speech voice will "
        "read verbatim -- kid-friendly, no block names, no punctuation "
        "that reads awkwardly aloud (no numbered lists, no markdown). This "
        "is DIFFERENT from message/summary/fault: that field is the fuller "
        "on-screen text; this is only the short version a voice actually "
        "says."
    ),
)


class DebugTurnKind(str, Enum):
    # ONE clarifying question, asked only when the student's report is
    # genuinely too vague to diagnose (no symptom described, no photo) --
    # no `fault`, no blocks. The turn immediately after this MUST be
    # `answer`, never a second `ask` — see _DEBUG_EXTRA in
    # app/llm/pipeline.py.
    ask = "ask"
    # The fault, stated directly, plus the corrected blocks -- the only
    # kind Debug had before this existed. Once the fault is known it is
    # always stated outright here, never turned into a second question --
    # that's the one rule from Debug's original single-shot design that
    # does NOT change.
    answer = "answer"


class DebugAnswer(BaseModel):
    """Debug asks AT MOST one clarifying question (kind=ask) before
    committing to a real diagnosis (kind=answer) -- reversed from the
    original single-shot-only design (Q16/grilling session: a coach's
    live feedback that students land here with a report too vague to
    diagnose, and never being asked anything left them stuck). What does
    NOT change: once the fault is actually known, Debug still states it
    directly -- it never makes the student guess it themselves, and never
    asks a second question after the first. `fault` is shown above the
    corrected blocks; a student who only sees corrected code learns
    nothing.

    `summary` doubles as the clarifying question's text when kind=ask
    (never a separate `question` field, to avoid a third near-synonym
    alongside `fault`) -- validate_answer enforces which fields matter for
    which kind, the same way BuildAnswer.message already covers ask/phase/
    complete with one field."""

    kind: DebugTurnKind
    voice: str = _VOICE_FIELD
    summary: str = Field(max_length=320)
    # Default "" (not required content) specifically so kind=ask doesn't
    # need to fabricate fault text before the fault is even known --
    # schema_for() force-adds this key back into `required` regardless
    # (same reasoning as BuildAnswer.phase_number/phase_total's own
    # comment), so a kind=answer turn still can't silently omit it.
    fault: str = Field(default="", max_length=280)
    blocks: list[ProgramBlock]
    note: Optional[str] = Field(default=None, max_length=140)
    image_note: Optional[str] = _IMAGE_NOTE_FIELD


class BuildTurnKind(str, Enum):
    ask = "ask"  # a clarifying question, or a plan needing confirmation — no blocks yet
    phase = "phase"  # this phase's blocks; more phases follow
    complete = "complete"  # the final (or only) phase — nothing more to confirm


class BuildAnswer(BaseModel):
    """Build follows the vendored skill's protocol: understand what the
    student wants (asking one focused question at a time if needed), confirm
    the plan, then build phase by phase for a long program — never dump the
    whole thing at once. A short program may go straight to `complete`.

    Deliberately a flat schema with one `kind` discriminator rather than a
    Pydantic discriminated union (`oneOf` in JSON Schema) — `oneOf` support
    in grammar-constrained decoding backends is inconsistent, and a flat
    shape with kind-conditional fields is verified to work against Ollama.
    See app/schema/validate.py for the kind-conditional checks this implies
    (e.g. `blocks` must be empty when `kind == "ask"`).
    """

    kind: BuildTurnKind
    voice: str = _VOICE_FIELD
    # 400 (the original limit) was tight enough to bite in practice: caught
    # live on Ollama, whose `format` parameter is TRUE grammar-constrained
    # decoding, not a hint — it enforced maxLength literally, hard-stopping
    # generation mid-word mid-sentence exactly at the 400th character
    # (Gemini/OpenRouter's looser enforcement wouldn't hit this the same
    # way; app/llm/pipeline.py's _truncate_oversized_strings is the
    # safety net for THAT direction, but there's no safety net for a
    # backend that truncates too early — the model just never got the
    # chance to finish its sentence). A plan-confirmation message with a
    # few bullet points for a 2-3 phase build genuinely needs more than
    # 400 characters to land on a natural stopping point.
    message: str = Field(
        max_length=700,
        description=(
            "The clarifying question (kind=ask), this phase's intro "
            "(kind=phase), or the final summary (kind=complete). Stay under "
            "700 characters and land on a complete sentence — this field "
            "hard-stops at the limit mid-generation on some backends, so "
            "running long risks getting cut off mid-word rather than "
            "wrapping up early."
        ),
    )
    phase_number: Optional[int] = Field(default=None, ge=1)
    phase_total: Optional[int] = Field(default=None, ge=1)
    # Deliberately NO default (`= Field(default_factory=list)` used to be
    # here) -- a field with a default is, by Pydantic's own JSON Schema
    # generation, never listed in `required`, which is exactly what let a
    # model skip emitting `blocks` at all for a real phase/complete turn.
    # Caught live: every phase/complete turn in a whole multi-turn build
    # came back with NO `blocks` key whatsoever in the raw JSON (not an
    # empty array present, just entirely absent) -- Pydantic's default
    # silently filled it in as `[]` on our side, so the answer still
    # "validated" as far as our own parsing went, while the model had
    # never actually been asked to produce anything there at all. Every
    # call site in this codebase already passes `blocks=` explicitly (a
    # grep across app/ and tests/ found zero that relied on the old
    # default), so this is a schema-only change with no other code impact
    # -- DebugAnswer's `blocks` (also no default) already worked this way,
    # confirmed via its own schema's `required` list including "blocks".
    blocks: list[ProgramBlock]
    note: Optional[str] = Field(default=None, max_length=140)
    image_note: Optional[str] = _IMAGE_NOTE_FIELD


# For typing only — Build and Debug are deliberately unrelated schemas (see
# BuildAnswer's docstring), not a shared base class, so anything that just
# needs `.blocks` (render_tree, validate_answer) takes this union.
Answer = Union[BuildAnswer, DebugAnswer]


def _require_every_program_block_field(schema: dict) -> dict:
    """Force all five ProgramBlock fields into the emitted schema's
    `required` list, for both modes.

    Same root cause as the `blocks` fix (see BuildAnswer.blocks's comment):
    every one of `slots`/`conditions`/`children`/`else_children` carries a
    Pydantic `default_factory=list`, so Pydantic omits them from `required`
    -- which told the model, correctly per the schema, that it could just
    not emit them. It did exactly that, and the result was a whole family
    of recurring live errors that all trace back to this ONE hole:
      - "expected 1 condition slot(s) for 'if then', got 0"  (conditions omitted)
      - "'Forever' wraps a block sequence but has none"       (children omitted)
      - "'Run function' wraps a block sequence but has none"  (children omitted)
      - "expected slots [...] got []"                         (slots omitted)
    Making the KEYS required doesn't force non-empty values -- a real leaf
    block still legitimately sends `"children": []`. It removes the option
    of silently skipping the field, which is what a model under strict
    decoding was taking. The Python defaults stay exactly as they are, so
    no call site anywhere changes.
    """
    block_schema = schema.get("$defs", {}).get("ProgramBlock")
    if block_schema is not None:
        block_schema["required"] = ["name", "slots", "conditions", "children", "else_children"]
    return schema


def schema_for(mode: str) -> dict:
    """The JSON Schema passed to Ollama's `format` parameter."""
    if mode == "build":
        schema = _require_every_program_block_field(BuildAnswer.model_json_schema())
        # phase_number/phase_total stay Optional[int] (default None) in the
        # Python model -- correct, since they're genuinely inapplicable for
        # kind=ask/complete, and changing that would mean touching every
        # BuildAnswer(...) call site across the test suite (~26 of them)
        # plus the frontend's `!= null` checks (index.html) for a field
        # that's legitimately nullable in Python. But a Pydantic default
        # means Optional fields are never listed in `required` (the exact
        # mechanism the `blocks` fix — see BuildAnswer.blocks's own
        # comment — addressed by removing blocks' default entirely).
        # Forcing presence here without changing nullability keeps every
        # other call site untouched: the key must appear (pushing a model
        # under strict/grammar-constrained decoding to actually engage
        # with filling it in for a real phase turn) while `null` remains a
        # legal value for kind=ask/complete, where it truly means nothing.
        # Caught live: a `kind: "phase"` turn that built real blocks (a
        # genuine improvement over the blocks-omitted failure) still came
        # back with both fields missing outright.
        schema["required"] = list(schema.get("required", [])) + ["phase_number", "phase_total"]
        return schema
    if mode == "debug":
        schema = _require_every_program_block_field(DebugAnswer.model_json_schema())
        # `fault` carries a Python-side default ("") specifically so
        # kind=ask doesn't need to fabricate fault text -- but per
        # BuildAnswer.blocks's own comment, ANY Pydantic default drops a
        # field from the emitted schema's `required` list, which is
        # exactly what previously let a model skip real fields silently.
        # Force the KEY back into `required` so a kind=answer turn still
        # can't omit it outright; `""` stays a legal value only for
        # kind=ask, enforced by validate_answer, not by the schema itself.
        schema["required"] = list(schema.get("required", [])) + ["fault"]
        return schema
    raise ValueError(f"unknown mode {mode!r}, expected 'build' or 'debug'")
