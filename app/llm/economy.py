"""Economy mode: serve a matched skill-doc's own content directly to the
student, with NO LLM call PER REQUEST — not even a cheap one. The whole
point is cost/latency, so the "wrapper message" a student sees is built
from teacher-authored text already sitting in the skill-doc
(`Function.usage_note`, app/skills/models.py), not generated per-request.
The one exception, and it stays an exception: `EconomyAnswer.render`, the
actual block visualisation, is read from `SkillDoc.demo_render` -- a field
computed by ONE AI call at admin save-time (app/skills/demo.py), a coach's
rare action, not a student's. Every real student request here still costs
nothing but a read.

Build only (app/api/main.py never routes Debug requests here) — Debug is
inherently about diagnosing a student's own specific broken code, which no
amount of matched reference material can substitute for.

A miss (no skill-doc matches the question well) is not a failure here: the
caller falls back to the normal full-AI path for that turn. Economy mode
only ever short-circuits when there's something real to hand back.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from app.skills.models import SkillDoc


@dataclass
class EconomyAnswer:
    skill_id: str
    skill_name: str
    message: str
    code_text: str
    # A save-time render of the skill's own reference program as real R
    # Block JSON (app/skills/demo.py), already computed and stored on the
    # SkillDoc -- reading it here costs nothing further, no LLM call. Empty
    # for a skill saved before this existed; the frontend falls back to
    # `code_text` (index.html's CodeBlock) when this is empty.
    render: list[dict] = field(default_factory=list)


def _code_text(doc: SkillDoc) -> str:
    """The skill's functions rendered as plain, student-readable pseudocode
    — deliberately NOT the model-facing prose from app/skills/render.py
    (that carries internal instructions like the MAIN LOOP/REUSABLE
    FUNCTIONS labelling and the chain-nesting warning, meant for the model,
    not a student)."""
    parts = []
    for fn in doc.functions:
        parts.append(f"{fn.name}:")
        for line in fn.body.splitlines() or [""]:
            parts.append(f"  {line}" if line.strip() else "")
        parts.append("")
    return "\n".join(parts).strip() + "\n"


def _message(doc: SkillDoc) -> str:
    """The student-facing intro. Built entirely from teacher-authored text
    (the main function's `usage_note`) — never generated, per Economy
    mode's whole reason for existing. Falls back to a plain, honest
    placeholder when a teacher hasn't written one yet, rather than
    inventing something that reads as if it came from the AI."""
    if doc.functions and doc.functions[0].usage_note:
        return doc.functions[0].usage_note
    return f"Here's the {doc.name} pattern. Ask a follow-up question if you'd like help adapting it."


def build_economy_answer(doc: SkillDoc) -> EconomyAnswer:
    return EconomyAnswer(
        skill_id=doc.id,
        skill_name=doc.name,
        message=_message(doc),
        code_text=_code_text(doc),
        render=doc.demo_render,
    )


async def try_economy_answer(
    question_text: str,
    skills: list[SkillDoc],
    embedder=None,
) -> Optional[EconomyAnswer]:
    """None means "no good match" — the caller (app/api/main.py) falls back
    to the full AI path for this turn; it is not an error."""
    from app.skills.retrieve import retrieve as retrieve_skill

    matched = await retrieve_skill(question_text, skills, embedder)
    if matched is None:
        return None
    return build_economy_answer(matched)
