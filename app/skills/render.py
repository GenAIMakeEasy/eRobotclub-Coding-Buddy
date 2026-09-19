"""Turns a teacher-authored SkillDoc into the prose the model actually reads
as context: a name, an optional short description, then each function's
name and pseudocode body.

The FIRST function is treated as the main-loop entry point, not a real
R Block function — its body is what should become a `Forever` block's
`children` directly, never wrapped in a `Create a function`/`Call function`
pair. Every function after it IS a real reusable R Block function. This
distinction matters: caught live, a model given no signal for which was
which produced BOTH a garbled attempt to cram the main loop's logic as
literal text into `Forever`'s own `slots`, AND a second, correctly-nested
copy of the same logic sitting as loose top-level blocks outside the loop
entirely — it hedged between two readings instead of picking the right one,
because nothing told it there was a right one.

The structural warning about how a multi-branch if/elif/.../else chain must
nest in R Block's own containment shape (each branch sits in the PREVIOUS
branch's else_children, one level deeper each time; a compound AND/OR
condition is itself a `Logical And / Or` block holding its own two operand
blocks) is fixed boilerplate here, appended whenever a skill-doc has any
functions at all — a teacher's pseudocode can't be relied on to restate it,
and it's a fact about the Answer Contract's containment rule
(app/schema/answer_contract.py), true for every skill-doc, not any one
teacher's wording.
"""

from __future__ import annotations

from app.skills.models import SkillDoc

_CHAIN_NESTING_NOTE = (
    "Any if/elif/.../else chain above is ONE chain, not separate standalone "
    "`if` blocks — only one branch ever runs per cycle. In R Block's own "
    "containment shape, each `elif`/`else if` is a fresh `if then` block "
    "nested inside the PREVIOUS one's `else_children`, going one level "
    "deeper each time. A condition combining two sensor checks with "
    "AND/OR is itself a `Logical And / Or` block seated in that `if "
    "then`'s `conditions`, holding its own two operand blocks in ITS "
    "`conditions` — never flatten those operands' slots onto the outer "
    "block. Never emit the chain's checks as sibling blocks at the same "
    "level; a sibling `if then` with no `else_children` link back is not a "
    "chain, and would run more than one branch's actions in the same "
    "cycle."
)


def _append_body(lines: list[str], fn) -> None:
    for line in fn.body.splitlines() or [""]:
        lines.append(f"  {line}" if line.strip() else "")
    if fn.usage_note:
        lines.append(f"  # tuning note for the student: {fn.usage_note}")


def render_skill(doc: SkillDoc) -> str:
    lines: list[str] = [f"# {doc.name}", ""]
    if doc.intro:
        lines += [doc.intro, ""]

    functions = doc.functions
    if functions:
        entry, rest = functions[0], functions[1:]
        lines.append(
            f'MAIN LOOP — "{entry.name}" below is what runs every cycle. Build it as a '
            "`Forever` block whose `children` directly hold this logic, inlined — this "
            "is a loop body, NOT a `Create a function` / `Call function` block pair, "
            "and there is no block named after it anywhere in the program."
        )
        lines.append("")
        _append_body(lines, entry)
        lines.append("")

        if rest:
            lines.append(
                "REUSABLE FUNCTIONS — each one below IS a real R Block function, "
                "DEFINED ONCE using TWO blocks together: a `Create a function` "
                "(no slots, no children — it has no body of its own) immediately "
                "followed by a `Run function` block whose `slots` name it and "
                "whose `children` hold its ACTUAL steps below, nested inside — "
                "`Run function` is never left with empty `children`, the same "
                "rule as any other wrapper block. Then wherever its name appears "
                "as `call <name>` in the pseudocode, place a SEPARATE `Call "
                "function` block (no children of its own — it just references "
                "the name) instead of repeating the steps inline again."
            )
            lines.append("")
            for fn in rest:
                lines.append(f"function {fn.name}:")
                _append_body(lines, fn)
                lines.append("")

        lines.append(_CHAIN_NESTING_NOTE)

    return "\n".join(lines).strip() + "\n"
