"""The data shape for a teacher-editable Ollo coding skill-doc — a reusable
strategy pattern (line-following, obstacle avoidance, ...) injected into the
model's context when a student's question matches it (app/skills/retrieve.py
picks which one; app/skills/render.py turns it into the prose the model
actually reads).

A skill-doc is just a name plus a set of named functions, each with a plain
pseudocode body a teacher writes free-form (if/elif/else, variable
assignments, calls to other functions by name) — no separate structured
variables/branches/main-logic sections. The first version of this had those
as separate structured fields (a "safer" shape, harder to corrupt) but that
made the edit form too complex for what's actually needed: this is prose
context for the model to read, not something our own code parses or
executes, so free text a teacher can write like they'd sketch pseudocode on
a whiteboard is both simpler to edit and no less safe.

Deliberately plain dataclasses, not the Pydantic Answer Contract models
(app/schema/answer_contract.py) — a skill-doc is never sent to the model as
constrained-decoding JSON Schema. Request-body validation for the admin CRUD
endpoints lives in app/api/main.py instead (SkillDocIn), which is where
untrusted input actually enters the system.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass
class Function:
    name: str
    body: str = ""
    # Teacher-authored, student-facing (unlike `body`, which is model
    # context only) — e.g. "Adjust IR and SPEED for your own track before
    # running." Shown as-is in Economy mode (app/llm/economy.py), which
    # serves this text directly with no LLM call at all, so it needs to
    # already read like something written for a student, not a model.
    usage_note: str = ""


@dataclass
class SkillDoc:
    id: str
    name: str
    # Keyword fallback used only when the embedding backend is unreachable
    # (app/skills/retrieve.py) — not the primary retrieval path.
    triggers: list[str] = field(default_factory=list)
    intro: str = ""
    functions: list[Function] = field(default_factory=list)
    # Recomputed by the admin API on every save (app/skills/retrieve.py's
    # compute_embedding) — never hand-edited, not exposed in the admin form.
    embedding: list[float] = field(default_factory=list)
    # A one-time, save-time render of this skill's own reference program as
    # real R Block JSON (app/skills/demo.py's render_demo_blocks, already in
    # the exact shape app/schema/render.py's render_tree produces) — so
    # Economy mode (app/llm/economy.py) can show actual blocks to a student
    # with ZERO further LLM calls per request, the one AI call already paid
    # for at save time (a coach's action, not a student's). Empty for a
    # skill saved before this existed, or if that render attempt failed —
    # both fall back to the plain pseudocode text that already worked.
    demo_render: list[dict] = field(default_factory=list)
    updated_at: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "SkillDoc":
        functions = [
            Function(name=f["name"], body=f.get("body", ""), usage_note=f.get("usage_note", ""))
            for f in d.get("functions", [])
        ]
        return SkillDoc(
            id=d["id"],
            name=d.get("name", d["id"]),
            triggers=list(d.get("triggers", [])),
            intro=d.get("intro", ""),
            functions=functions,
            embedding=list(d.get("embedding", [])),
            demo_render=list(d.get("demo_render", [])),
            updated_at=d.get("updated_at", ""),
        )
