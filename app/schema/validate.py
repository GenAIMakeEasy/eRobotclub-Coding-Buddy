"""Second-order validation of a parsed Answer.

The `ProgramBlock.name` enum already makes an invented block name structurally
impossible (ADR-0001). What it cannot catch is a REAL block used in the wrong
structural role — e.g. `Repeat time(s)` (a wrapper) emitted with no
`children`, or a plain statement block given a `condition` it has no slot
for. This module catches that second-order error.

Per the grilling session (Q16): a failing block is shown to the student in a
red box, not retried — a retry under a serialised inference queue costs
wall-clock time exactly when the class is busiest.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.schema import block_directory as bd
from app.schema.answer_contract import Answer, BuildAnswer, BuildTurnKind, DebugAnswer, DebugTurnKind, ProgramBlock


@dataclass
class BlockError:
    """One structural fault, addressed by position so the UI can red-box the
    exact block rather than the whole answer."""

    path: tuple[int, ...]
    block_name: str
    message: str


@dataclass
class ValidationResult:
    errors: list[BlockError] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def validate_answer(answer: Answer) -> ValidationResult:
    result = ValidationResult()

    if isinstance(answer, BuildAnswer):
        _validate_build_kind(answer, result)
    elif isinstance(answer, DebugAnswer):
        _validate_debug_kind(answer, result)

    for i, block in enumerate(answer.blocks):
        _validate_block(block, path=(i,), result=result)
    return result


def _validate_build_kind(answer: BuildAnswer, result: ValidationResult) -> None:
    """Kind-conditional checks the flat BuildAnswer schema can't express
    itself (see BuildAnswer's docstring on why it's flat, not a discriminated
    union): a clarifying question shouldn't carry blocks the student would
    have no context for yet, and a phase must say which phase it is."""
    if answer.kind == BuildTurnKind.ask and answer.blocks:
        result.errors.append(BlockError((), "", "kind=ask must not include blocks — ask first, build after"))

    if answer.kind == BuildTurnKind.phase:
        if answer.phase_number is None or answer.phase_total is None:
            result.errors.append(BlockError((), "", "kind=phase requires phase_number and phase_total"))
        elif answer.phase_number > answer.phase_total:
            result.errors.append(BlockError((), "", "phase_number cannot exceed phase_total"))

    if answer.kind in (BuildTurnKind.phase, BuildTurnKind.complete) and not answer.blocks:
        # Observed live for `complete`: the model chose kind=complete with an
        # empty program and a confused, off-topic message instead of
        # correctly asking a clarifying question (kind=ask). Observed live
        # AGAIN, separately, for `phase`: a phase turn whose message
        # confidently described real blocks ("we'll handle turning left...")
        # while `blocks` was actually empty -- validation passed (only
        # `complete` was checked here), so the student's app just rendered
        # an empty block panel with no error at all, no code, nothing to
        # place. Either kind's entire job is delivering blocks for this
        # turn; an empty one is never legitimate for either — that's what
        # kind=ask is for.
        result.errors.append(
            BlockError((), "", f"kind={answer.kind.value} has no blocks — this should have been kind=ask instead")
        )


def _validate_debug_kind(answer: DebugAnswer, result: ValidationResult) -> None:
    """Kind-conditional checks for Debug's own two-kind shape (see
    DebugAnswer's docstring for the reversal this supports): a clarifying
    question has nothing to diagnose yet, so no blocks; a real answer's
    entire job is stating the fault and showing the fix, so both must be
    real content, the same "empty isn't legitimate" reasoning
    _validate_build_kind already applies to phase/complete."""
    if answer.kind == DebugTurnKind.ask and answer.blocks:
        result.errors.append(BlockError((), "", "kind=ask must not include blocks — ask first, diagnose after"))

    if answer.kind == DebugTurnKind.answer and not answer.fault.strip():
        result.errors.append(
            BlockError((), "", "kind=answer requires a real `fault` — a diagnosis with no stated cause teaches nothing")
        )

    if answer.kind == DebugTurnKind.answer and not answer.blocks:
        result.errors.append(
            BlockError((), "", "kind=answer has no blocks — this should have been kind=ask instead")
        )


def _validate_block(block: ProgramBlock, path: tuple[int, ...], result: ValidationResult) -> None:
    name = block.name.value
    catalog = bd.by_name().get(name)

    if catalog is None:
        # Unreachable when Ollama's format enforces the enum; not unreachable
        # in tests or a backend that doesn't support constrained decoding.
        result.errors.append(BlockError(path, name, f"{name!r} is not a real R Block"))
        return

    expected_kinds = catalog.slot_kinds
    actual_kinds = [s.kind.value for s in block.slots]
    if actual_kinds != expected_kinds:
        result.errors.append(
            BlockError(
                path,
                name,
                f"expected slots {expected_kinds!r} for {name!r}, got {actual_kinds!r}",
            )
        )

    expected_conditions = catalog.condition_count
    actual_conditions = len(block.conditions)
    if actual_conditions != expected_conditions:
        result.errors.append(
            BlockError(
                path,
                name,
                f"expected {expected_conditions} condition slot(s) for {name!r}, got {actual_conditions}",
            )
        )
    for k, cond in enumerate(block.conditions):
        _validate_block(cond, path + (0, k), result)

    # `Forever` wrapping another `Forever` (and nothing else) is never
    # legitimate at any depth -- caught live, a real answer nested `Forever`
    # inside itself a dozen-plus levels deep, structurally valid per the
    # schema (each one really does wrap a real block sequence, so nothing
    # above catches it) but obviously nonsensical: the inner loop would
    # never even get a chance to run, since the outer one restarts before
    # ever reaching whatever comes after it. Unlike `Repeat time(s)`, which
    # CAN legitimately nest the same block for a real grid/pattern loop,
    # an infinite loop inside an infinite loop is a contradiction, not a
    # design choice -- narrowly scoped to `Forever` specifically rather
    # than any wrapper nesting itself, to avoid flagging that legitimate
    # nested-`Repeat` case.
    if name == "Forever" and len(block.children) == 1 and block.children[0].name.value == "Forever":
        result.errors.append(
            BlockError(path, name, "'Forever' should never wrap another 'Forever' directly — merge them into one loop")
        )

    if block.children and not catalog.has_children:
        result.errors.append(
            BlockError(path, name, f"{name!r} does not wrap a block sequence")
        )
    elif catalog.has_children and not block.children:
        # Observed live (Gemini): a "complete" Build answer's only block was
        # `Forever` with an empty `children` — no error, kind=complete, a
        # confident-sounding message ("Here is your line tracing
        # program!"), and a student left staring at a loop that does
        # nothing. A wrapper with nothing in it is never a real program,
        # so this is exactly the "real block, wrong shape" case this
        # module's own docstring named as the motivating example.
        result.errors.append(
            BlockError(path, name, f"{name!r} wraps a block sequence but has none — it would do nothing")
        )
    for j, child in enumerate(block.children):
        _validate_block(child, path + (1, j), result)

    if block.else_children and not catalog.has_else:
        result.errors.append(
            BlockError(path, name, f"{name!r} has no else branch")
        )
    for j, child in enumerate(block.else_children):
        _validate_block(child, path + (2, j), result)
