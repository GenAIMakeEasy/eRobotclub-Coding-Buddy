"""Pre-renders a skill-doc's own reference program into real R Block JSON,
ONCE at admin save-time — the same shape app/schema/render.py's render_tree
produces for a live Build/Debug answer, so Economy mode
(app/llm/economy.py) can show a student actual blocks (BlockPanel/CBlock,
the same component a real answer uses), not plain pseudocode text, while
still making ZERO further LLM calls per student request.

This is the one deliberate exception to Economy mode's "no LLM call, not
even a cheap one" rule (see economy.py's own docstring): the AI call here
happens once per skill SAVE, a coach's rare action, never per matched
student question. Reuses the exact same Build pipeline
(app/llm/pipeline.py's answer()) a live request would use, rather than a
separate/simpler prompt, so a coach's preview and a student's real answer
are held to the identical Answer Contract, schema, and validation --
INCLUDING the phase-by-phase protocol (BuildAnswer's own docstring: "never
dump the whole thing at once"). Caught live: an earlier version of this
forced kind=complete outright ("no phases, build it all in one turn") for
the real line-following skill (7 functions, a six-branch chain) and the
model's single giant generation degenerated into a RecursionError while
parsing the resulting JSON -- almost certainly a decoding repetition loop,
the same class of failure app/llm/ollama_client.py's own docstring already
documents for long structured output. Letting the model use kind=phase here
too, the same as it would for a real student's equally long request,
produces several smaller generations instead of one enormous one.
"""

from __future__ import annotations

from app.llm.ollama_client import ChatClient
from app.llm.pipeline import answer as run_pipeline
from app.schema.answer_contract import BuildAnswer
from app.schema.render import render_tree
from app.skills.models import SkillDoc
from app.skills.render import render_skill

_FIRST_QUESTION = (
    "Build the reference R Block program described by the coaching skill "
    "below, EXACTLY as its pseudocode and structural rules describe, "
    "following the normal Build protocol -- if it's long, build it phase "
    "by phase (kind=phase) rather than as one giant turn; a short program "
    "can go straight to kind=complete. Do not respond with kind=ask: the "
    "skill doc below already answers everything a clarifying question "
    "could ask, and there is no student here to ask.\n\n"
)

_CONTINUE_REPLY = "Good, continue with the next phase."

_ASK_NUDGE = (
    "The skill doc already answers everything -- do not ask a question. "
    "Start building now: kind=phase for the first phase (if this is long), "
    "or kind=complete (if the whole thing is actually short), with real "
    "blocks either way."
)

_ERROR_NUDGE_TEMPLATE = (
    "That last turn had these structural errors -- fix every one of them, "
    "following the containment rules in the R Block Reference exactly (a "
    "condition block seats INSIDE the wrapper block's `conditions`, never "
    "as a sibling block next to it):\n{errors}"
)

# One shared budget for both recoverable failure kinds (a stray kind=ask, or
# a turn with structural errors) across the whole build, not per-kind --
# either one repeatedly is a sign this attempt isn't converging, and a
# text-only fallback (see render_demo_blocks's own docstring) is better
# than an unbounded number of extra calls chasing a fix that isn't coming.
#
# Kept deliberately tight (this and _MAX_TURNS below): caught live, a
# single one of these calls against a real complex skill (7 functions, a
# six-branch chain) routinely ran 30-90+ seconds each, over OpenRouter --
# this whole system prompt is the full R Block Reference resent every
# turn, not a cheap chat message. A coach waiting on a save would rather
# get a fast, honest fallback to plain pseudocode text than an occasional
# multi-minute hang chasing one more retry that might not even converge.
_RECOVERY_BUDGET = 1
# Genuine phase turns plus recoveries. A real multi-phase build for even a
# fairly involved skill is usually 2-4 phases; this caps the worst case
# without assuming every skill is that simple.
_MAX_TURNS = 4


async def render_demo_blocks(doc: SkillDoc, client: ChatClient) -> list[dict]:
    """Best-effort: returns `[]` (never raises for a model-quality problem)
    if the model doesn't converge on a clean kind=complete within the turn
    budget above, or any accepted turn has a structural validation error --
    caught live: a turn can be kind=complete with real (non-empty) blocks
    and STILL have every `if then`'s condition sitting as a stray sibling
    instead of seated in `conditions` (render_tree renders that as a
    visible red "expected 1 condition slot(s) ... got 0" box). That's
    tolerable on a live student turn -- Q16's whole reasoning is to show
    it, not hide it -- but this is a REFERENCE program a coach is
    pre-approving for every future student who matches this skill; shipping
    it broken defeats the entire point of pre-rendering it. Falling back to
    the plain pseudocode text (the behaviour before this feature existed)
    is strictly better than a confusing red-boxed "reference".
    `ModelUnavailable` (a real backend/network failure) is deliberately
    left to propagate -- the caller decides how to degrade, the same as it
    already does for the embedder. A `RecursionError` while parsing a
    turn's JSON (the exact failure caught live before phase-by-phase
    generation was added -- see this module's own docstring) bails out to
    `[]` immediately rather than retrying: every call here runs at
    temperature=0, so retrying the SAME question would deterministically
    reproduce the same degenerate output, not recover from it."""
    history: list[dict] = []
    question = _FIRST_QUESTION + render_skill(doc)
    all_blocks: list = []
    recoveries_left = _RECOVERY_BUDGET

    for _ in range(_MAX_TURNS):
        try:
            result = await run_pipeline(client, mode="build", question=question, history=history or None)
        except RecursionError:
            return []
        answer = result.answer
        kind = getattr(answer, "kind", None) if answer is not None else None

        if kind == "ask" or (answer is not None and result.errors):
            if recoveries_left <= 0:
                return []
            recoveries_left -= 1
            history.append({"role": "user", "content": question})
            history.append({"role": "assistant", "content": answer.message})
            if kind == "ask":
                question = _ASK_NUDGE
            else:
                error_lines = "\n".join(f"- {e.message}" for e in result.errors[:6])
                question = _ERROR_NUDGE_TEMPLATE.format(errors=error_lines)
            continue

        if answer is None:
            return []

        all_blocks.extend(answer.blocks)

        if kind == "complete":
            if not all_blocks:
                return []
            final = BuildAnswer(kind="complete", voice=answer.voice, message=answer.message, blocks=all_blocks)
            return render_tree(final, [])

        # kind == "phase": accepted, no errors -- carry it forward and ask
        # for the next one, the same history shape app/api/main.py's own
        # _conversation_history builds for a real student thread.
        names = ", ".join(b.name.value for b in answer.blocks) if answer.blocks else "(none)"
        history.append({"role": "user", "content": question})
        history.append({"role": "assistant", "content": f"{answer.message}\nBlocks so far: {names}"})
        question = _CONTINUE_REPLY

    return []
