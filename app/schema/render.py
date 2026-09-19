"""Turn a validated Answer into the exact node shape the approved UI's block
components already render (design/design-demos/approved-v2.html: Pill, Oval,
Hex, Stmt, CBlock, BlockPanel). That mockup hand-authored its fixture trees
as ``{id, cat, p, cond, c, body, elseBody}`` — this module produces the same
shape from a real ProgramBlock, so the frontend stays a dumb renderer and
every fact about R Block's grammar (which block is a wrapper, where a
condition seats, how many slots a block takes) stays in Python, where it is
already tested against the vendored directory.

``p`` (parts) is exact_text's fixed text and placeholders, interleaved with
the block's real slot values, in the same tuple encoding the mockup used:
``('t', text)``, ``('d', text)`` dropdown, ``('v', text)`` value,
``('g',)`` the if-block's gear icon, ``('cond', k)`` where the k-th nested
condition block (``node["conds"][k]``) is spliced in — most blocks have at
most one, but ``Logical And / Or`` seats two (its left and right operand).
"""

from __future__ import annotations

import re
from typing import Any

from app.schema import block_directory as bd
from app.schema.answer_contract import Answer, ProgramBlock
from app.schema.validate import BlockError

_TOKEN = re.compile(r"\[[^\]]+\]|[^\[]+")


def _interleave(exact_text: str, slots: list, condition_count: int) -> list[list]:
    """Split exact_text into its fixed-text and placeholder tokens, then
    substitute real slot values for [Dropdown]/[Value] placeholders in
    order. Tolerant of a slot-count mismatch (a validation-failed block must
    still render something, not crash the page) — leftover placeholders
    render as an empty value; leftover slots are appended at the end.

    Each ``[Condition]``/``[Condition Slot]`` token becomes ``["cond", k]``
    with ``k`` counting up left to right — ``Logical And / Or`` has two such
    tokens, one per operand, so they need distinct indices rather than a
    single shared marker."""
    parts: list[list] = []
    slot_iter = iter(slots)
    condition_index = 0

    for raw in _TOKEN.findall(exact_text):
        if not raw.startswith("["):
            text = raw.strip()
            if text:
                parts.append(["t", text])
            continue

        inner = raw[1:-1]
        if inner == "Icon":
            parts.append(["g"])
        elif inner in ("Condition", "Condition Slot"):
            parts.append(["cond", condition_index])
            condition_index += 1
        else:
            slot = next(slot_iter, None)
            kind = slot.kind.value if slot else ("dropdown" if inner == "Dropdown" else "value")
            text = slot.text if slot else ""
            parts.append(["d" if kind == "dropdown" else "v", text])

    # A condition slot that Remarks describes but Exact Text never spells out
    # as a token (e.g. "Repeat until") — append it so it's still reachable.
    if condition_count and condition_index == 0:
        parts.append(["cond", 0])

    # Any slots the model supplied beyond what Exact Text accounts for
    # (a validation failure, not a normal case) — surface them rather than
    # silently dropping information a student might need to see.
    for slot in slot_iter:
        parts.append(["d" if slot.kind.value == "dropdown" else "v", slot.text])

    return parts


def _starts_with_if(node: dict[str, Any]) -> bool:
    """Whether a rendered node's own first visible word is "if"/"If" —
    several condition blocks are named that way (``If the IR sensor
    value``, ``If detected sound count``, ...), which reads as a doubled
    "if if" when seated right after ``if then``'s own literal "if" text.
    Recurses through a leading ``cond`` marker (not text/dropdown/value)
    so this also catches ``Logical And / Or``, whose own Exact Text
    doesn't start with "if" but whose FIRST seated operand often does."""
    for part in node.get("p", []):
        kind = part[0]
        if kind == "g":
            continue
        if kind == "t":
            return part[1].strip().lower().startswith("if")
        if kind == "cond":
            conds = node.get("conds") or []
            return part[1] < len(conds) and _starts_with_if(conds[part[1]])
        return False
    return False


def _drop_redundant_if(node: dict[str, Any]) -> None:
    """Mutates ``node["p"]`` in place: if a literal "if" text token is
    immediately followed by a condition marker whose own seated condition
    already starts with "if"/"If", drop the wrapper's redundant one —
    caught live: "if then" seated with "If the IR sensor value ..." reads
    "if If the IR sensor value..." (a real double "if"), which a teacher
    flagged as visibly wrong. This is presentation-only: the pinned block
    catalog (ADR-0003) and the Answer Contract's actual data are untouched,
    only how the two adjacent texts are displayed together."""
    p = node["p"]
    conds = node.get("conds") or []
    for i, part in enumerate(p):
        if part[0] == "t" and part[1].strip().lower() == "if" and i + 1 < len(p) and p[i + 1][0] == "cond":
            idx = p[i + 1][1]
            if idx < len(conds) and _starts_with_if(conds[idx]):
                del p[i]
            break


def _render_block(
    block: ProgramBlock,
    path: tuple[int, ...],
    error_messages: dict[tuple[int, ...], list[str]],
) -> dict[str, Any]:
    name = block.name.value
    catalog = bd.by_name().get(name)

    node: dict[str, Any] = {
        "id": "-".join(str(p) for p in path) or "root",
        "cat": catalog.tab if catalog else "Control",
        "p": (
            _interleave(catalog.exact_text, block.slots, catalog.condition_count if catalog else 0)
            if catalog
            else [["t", name]]
        ),
    }

    if path in error_messages:
        node["bad"] = True
        node["badMessages"] = error_messages[path]

    if block.conditions:
        node["conds"] = [
            _render_block(cond, path + (0, k), error_messages) for k, cond in enumerate(block.conditions)
        ]
        _drop_redundant_if(node)

    has_children = bool(block.children) or (catalog.has_children if catalog else False)
    if has_children:
        node["c"] = True
        node["body"] = [
            _render_block(child, path + (1, j), error_messages) for j, child in enumerate(block.children)
        ]

    if block.else_children:
        node["elseBody"] = [
            _render_block(child, path + (2, j), error_messages) for j, child in enumerate(block.else_children)
        ]

    return node


def render_tree(answer: Answer, errors: list[BlockError]) -> list[dict[str, Any]]:
    """The full render-ready tree for `answer.blocks`, with every path that
    failed validation carrying its error message(s) as `bad`/`badMessages`
    rather than being dropped — Q16: shown in a red box, not retried, not
    hidden."""
    error_messages: dict[tuple[int, ...], list[str]] = {}
    for e in errors:
        error_messages.setdefault(e.path, []).append(e.message)

    return [_render_block(b, (i,), error_messages) for i, b in enumerate(answer.blocks)]
