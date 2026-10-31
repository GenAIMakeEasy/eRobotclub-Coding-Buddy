"""The API surface. Five screens (design/spec.md) need three things from the
server: submit a question (POST /ask), watch it move through the queue and
get the result (GET /answer/{job_id}), and know the model backend is alive
(GET /health).

The heavy lifting — routing, the schema, validation, the queue, sessions,
logging — all already exists as standalone, independently tested modules.
This file's only job is wiring them together and translating between HTTP
and their shapes.

State lives on `app.state`, built fresh by `create_app()`, rather than as
module-level singletons — a shared queue across the whole test session lets
one test's cancelled-mid-flight job leak stuck state into an unrelated test.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Callable, Optional

import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError

from app import config
from app.admin.auth import AdminAuth
from app.site_auth import COOKIE_NAME, SiteAuth
from app.llm.economy import EconomyAnswer, build_economy_answer, try_economy_answer
from app.llm.gemini_client import GeminiClient
from app.llm.openrouter_client import OpenRouterClient
from app.llm.ollama_client import ChatClient, ModelUnavailable, OllamaClient
from app.llm import pipeline as pipeline_module
from app.llm.pipeline import PipelineResult, answer as run_pipeline, routing_text
from app.queue.manager import JobStatus, QueueFull, QueueManager
from app.rag.embeddings import OllamaEmbedder
from app.schema.render import render_tree
from app.session.log import LogWriter
from app.session.store import FileSessionStore, Session
from app.levels import store as levels_store
from app.schema import block_directory as block_directory_module
from app.prompts import store as prompts_store
from app.prompts.models import PromptOverrides
from app.skills import store as skills_store
from app.skills.demo import render_demo_blocks
from app.skills.models import Function, SkillDoc
from app.skills.render import render_skill
from app.skills.retrieve import compute_embedding
from app.tts.elevenlabs_client import ElevenLabsTTS


class AskRequest(BaseModel):
    device_id: str = Field(min_length=1, max_length=128)
    mode: str = Field(pattern="^(build|debug)$")
    language: str = "English"
    question: str = Field(min_length=1, max_length=2000)
    image_base64: Optional[str] = None
    # Build only (app/llm/economy.py) — Debug always ignores this, since
    # diagnosing a student's own broken code has no canned answer to fall
    # back to.
    economy: bool = False
    # True from index.html's `reply`/`followUp` (continuing an open thread
    # or reacting to an answer already on screen) -- False (the default)
    # from a fresh top-level question on the entry screen. Distinguishes
    # "extend/adjust what I just got" from "this is unrelated", which
    # session turns alone can't: a `kind=complete` turn deliberately closes
    # the thread (BuildAnswer's own docstring) so a genuinely new question
    # doesn't drag stale blocks into an unrelated build, but a student
    # asking to add one more thing to a program they just received needs
    # exactly that stale-looking context carried forward on purpose. See
    # _continuation_history_after_complete.
    continue_thread: bool = False
    # A levels_store.Level id picked from the entry screen's dropdown; its
    # admin-written prompt is added to the system prompt. Unknown/None
    # means no level (same as before this existed).
    level: Optional[str] = Field(default=None, max_length=128)


class LevelOut(BaseModel):
    id: str
    name: str


class LevelAdminIn(BaseModel):
    id: Optional[str] = Field(default=None, max_length=128)
    name: str = Field(min_length=1, max_length=60)
    prompt: str = Field(default="", max_length=20000)
    directory_keys: list[str] = Field(default_factory=list, max_length=100)


class DirectoryEntryIn(BaseModel):
    key: str = Field(min_length=1, max_length=60)
    value: str = Field(default="", max_length=50000)


class DirectoryActionOut(BaseModel):
    entries: list[DirectoryEntryIn]
    note: str


class LevelAdminOut(LevelAdminIn):
    id: str


class AskResponse(BaseModel):
    job_id: int
    position: int
    estimated_wait_seconds: Optional[list[float]] = None


class EconomyAnswerOut(BaseModel):
    skill_id: str
    skill_name: str
    message: str
    code_text: str
    # Real R Block JSON, pre-rendered at admin save-time (app/skills/demo.py)
    # -- empty for a skill saved before this existed, or if that render
    # attempt failed; the frontend falls back to `code_text` either way.
    render: list[dict[str, Any]] = Field(default_factory=list)


class AnswerResponse(BaseModel):
    status: str
    position: Optional[int] = None
    estimated_wait_seconds: Optional[list[float]] = None
    answer: Optional[dict[str, Any]] = None
    render: Optional[list[dict[str, Any]]] = None
    errors: Optional[list[dict[str, Any]]] = None
    error_message: Optional[str] = None
    economy: Optional[EconomyAnswerOut] = None
    # Set when this turn was quietly served by OPENROUTER_FALLBACK_MODEL
    # after the configured primary model failed (see openrouter_client.py's
    # chat()) -- a plain-language reason for the frontend to show as a
    # transient notice, not the raw exception text (that stays in the
    # server log for an admin, via LogWriter's own "model"/fallback fields).
    fallback_notice: Optional[str] = None


class AdminLoginRequest(BaseModel):
    password: str


class SiteLoginRequest(BaseModel):
    password: str


class AdminLoginResponse(BaseModel):
    token: str


class AdminSettingsResponse(BaseModel):
    backend: str
    ollama_model: str
    ollama_host: str
    gemini_model: str
    gemini_api_key_set: bool
    openrouter_model: str
    openrouter_api_key_set: bool
    # Independent of `backend` -- TTS for the assistant's own clarifying/
    # plan-confirmation questions, not one of the Build/Debug model choices.
    elevenlabs_api_key_set: bool
    elevenlabs_voice_id: str


class AdminSettingsUpdate(BaseModel):
    backend: str = Field(pattern="^(local|gemini|openrouter)$")
    ollama_model: Optional[str] = Field(default=None, max_length=200)
    gemini_model: Optional[str] = Field(default=None, max_length=200)
    gemini_api_key: Optional[str] = Field(default=None, max_length=500)
    openrouter_model: Optional[str] = Field(default=None, max_length=200)
    openrouter_api_key: Optional[str] = Field(default=None, max_length=500)
    elevenlabs_api_key: Optional[str] = Field(default=None, max_length=500)
    elevenlabs_voice_id: Optional[str] = Field(default=None, max_length=200)


class AdminRestartRequest(BaseModel):
    # False (the default) means "check first" -- /admin/restart 409s with
    # how many student requests are still in flight rather than dropping
    # them silently; the admin panel re-sends with force=True only after
    # showing that number to the coach and getting a confirm().
    force: bool = False


class AdminRestartResponse(BaseModel):
    restarting: bool
    active_jobs: int


class PromptsResponse(BaseModel):
    # EFFECTIVE active text (what's actually sent to the model right now)
    # plus the EFFECTIVE default (a teacher's own saved default via "Set as
    # default", or the hardcoded original if they've never set one) --
    # PromptOverrides' two-layer model, see its own docstring. `*_is_default`
    # is true when the active text equals the effective default (no active
    # override saved), regardless of whether that default is custom or
    # original. `default_*_is_custom` distinguishes "reset goes back to a
    # teacher's own saved default" from "reset goes back to the original
    # code text" -- the admin page shows this so "Reset to default" doesn't
    # read as a stronger promise (a return to the ORIGINAL) than it is.
    system_template: str
    system_template_is_default: bool
    default_system_template: str
    default_system_template_is_custom: bool
    build_extra: str
    build_extra_is_default: bool
    default_build_extra: str
    default_build_extra_is_custom: bool
    debug_extra: str
    debug_extra_is_default: bool
    default_debug_extra: str
    default_debug_extra_is_custom: bool


class PromptsUpdate(BaseModel):
    # None (the default, if omitted from the request) means "leave this
    # field unchanged" -- matches AdminSettingsUpdate's own convention for
    # optional fields. An explicit empty string means "reset to default"
    # (see the /admin/prompts handler), not "save an empty prompt" -- an
    # empty prompt would fail validate_prompt_override anyway.
    system_template: Optional[str] = Field(default=None, max_length=20000)
    build_extra: Optional[str] = Field(default=None, max_length=20000)
    debug_extra: Optional[str] = Field(default=None, max_length=20000)
    # "Set as default" -- promotes the accompanying field's text (or, if
    # that field is omitted, whatever is currently ACTIVE for it) into
    # being this field's new default. Mutually exclusive in practice with
    # sending "" for the same field in the same request (reset vs. set-as-
    # default), but nothing stops both being sent -- the handler applies
    # the plain value first, then the set-as-default promotion, so a
    # request combining both ends up with the new default AND the active
    # override cleared to match it (see the handler for the exact order).
    system_template_set_as_default: bool = False
    build_extra_set_as_default: bool = False
    debug_extra_set_as_default: bool = False
    # The escape hatch "Set as default" needs: once a field has its own
    # custom default, "Reset to default" only ever reaches THAT text --
    # this clears `<field>_default` back to None too, so the field goes
    # all the way back to the ORIGINAL text this app shipped with. Wins
    # over every other flag for the same field if sent together.
    system_template_restore_original: bool = False
    build_extra_restore_original: bool = False
    debug_extra_restore_original: bool = False


class TTSRequest(BaseModel):
    # Bounded the same way BuildAnswer.message already is (700 chars) --
    # this endpoint is only ever meant to speak one of the assistant's own
    # kind="ask" messages, never arbitrary long text, so a generous but
    # finite cap protects against a broken/malicious client running up
    # ElevenLabs' per-character billing.
    text: str = Field(min_length=1, max_length=800)


class FunctionIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    body: str = Field(default="", max_length=2000)
    # Teacher-authored, student-facing — shown as-is in Economy mode
    # (app/llm/economy.py) with no LLM call, unlike `body` (model context
    # only, never shown to a student directly).
    usage_note: str = Field(default="", max_length=400)


class SkillDocIn(BaseModel):
    """The admin panel's CRUD payload — a skill is just a name plus a set
    of named functions, each a plain pseudocode body a teacher writes
    free-form (if/elif/else, calls to other functions by name). Never
    carries `embedding`: that's recomputed server-side on every save
    (app/skills/retrieve.py's compute_embedding), not something a teacher
    hand-edits."""

    id: str = Field(min_length=1, max_length=60, pattern=r"^[a-z0-9-]+$")
    name: str = Field(min_length=1, max_length=100)
    triggers: list[str] = Field(default_factory=list)
    # Caught live: 1200 (the original cap) was already too tight for real
    # content -- the line-following skill's own intro (its "REQUIRED
    # PROGRAM SHAPE" rules, written earlier to fix a real student-facing
    # bug) runs to ~2000 characters, and GET /admin/skills started 500ing
    # outright the moment that intro existed, because SkillDocOut shares
    # this same constraint on the way back OUT, not just in. 3000 leaves
    # real headroom rather than picking a number just past today's content.
    intro: str = Field(default="", max_length=3000)
    functions: list[FunctionIn] = Field(default_factory=list)


class SkillPreviewOut(BaseModel):
    """What a save actually produces, in both places this skill-doc is
    ever used -- so a coach previewing a save sees the real thing, not a
    guess at it. `ai_context` is the exact prose render_skill() hands the
    model as context (app/skills/render.py, including its structural
    boilerplate); `student_message`/`student_code` is exactly what a
    student sees in Economy mode (app/llm/economy.py) with no LLM call at
    all -- the same two functions the live paths call, not a re-implementation."""

    ai_context: str
    student_message: str
    student_code: str
    # Same data as EconomyAnswerOut.render -- real R Block JSON a coach can
    # see rendered as actual blocks, not just pseudocode text, right after
    # saving.
    student_render: list[dict[str, Any]] = Field(default_factory=list)


class SkillDocOut(SkillDocIn):
    updated_at: str = ""
    # Only ever populated on a save response -- listing many skills
    # (GET /admin/skills) has no reason to pay for computing a preview of
    # each one.
    preview: Optional[SkillPreviewOut] = None


def _skill_from_payload(payload: SkillDocIn) -> SkillDoc:
    return SkillDoc(
        id=payload.id,
        name=payload.name,
        triggers=list(payload.triggers),
        intro=payload.intro,
        functions=[Function(name=f.name, body=f.body, usage_note=f.usage_note) for f in payload.functions],
    )


def _skill_to_out(doc: SkillDoc) -> SkillDocOut:
    return SkillDocOut(
        id=doc.id,
        name=doc.name,
        triggers=doc.triggers,
        intro=doc.intro,
        functions=[FunctionIn(name=f.name, body=f.body, usage_note=f.usage_note) for f in doc.functions],
        updated_at=doc.updated_at,
    )


class SkillDraftRequest(BaseModel):
    """A coach's raw material for drafting a new skill: a photo of a
    reference program (the same image_base64 shape /ask already accepts),
    plus an optional note the photo alone can't convey (e.g. which sensor
    ports, or a rule the coach wants kept even though it's not visible)."""

    image_base64: str = Field(min_length=1)
    note: str = Field(default="", max_length=300)


class SkillDraftFunctionOut(BaseModel):
    # Field(...) with no `default=` is required in the generated JSON
    # Schema -- unlike FunctionIn's fields above, this schema is sent
    # straight to the model as its `format`, so leaving `name`/`body`
    # required (rather than defaulted) is what stops it from skipping
    # them, the same reasoning as answer_contract.py's `blocks` fix.
    name: str = Field(max_length=60)
    body: str = Field(max_length=2000)
    usage_note: str = Field(default="", max_length=400)


class SkillDraftOut(BaseModel):
    """A DRAFT for a coach to review and edit in the editor -- never
    written to the skills store directly (there's no endpoint that saves
    this as-is). Same shape as SkillDocIn minus `id`, which the coach
    picks themselves."""

    name: str = Field(max_length=100)
    intro: str = Field(default="", max_length=3000)  # matches SkillDocIn.intro's own cap, see its comment
    triggers: list[str] = Field(default_factory=list)
    functions: list[SkillDraftFunctionOut]


_SKILL_DRAFT_SYSTEM_PROMPT = """You are helping a robotics coach turn a photo of a reference ROBOTIS R+ Ollo block program into a reusable skill doc for the R Block Coding Buddy app's skill library. A student's question later gets matched against these skill docs and shown this content directly (or used as context by the assistant), so read the photo's blocks carefully and describe the SAME logic and structure actually shown -- do not invent behaviour the photo doesn't show, and do not simplify away a branch or condition that is visibly there.

Write:
- `name`: a short, plain name for the technique (e.g. "Line Following").
- `intro`: 2-4 sentences a coach would want every student to see before building this -- fixed rules the photo makes clear (which sensors/ports, any required order or threshold), not generic advice.
- `triggers`: 4-8 lowercase keyword phrases a student might type that match this technique.
- `functions`: plain pseudocode, written like a coach sketching it on a whiteboard -- if/elif/else, `call OTHER_FUNCTION` to reuse another function listed below it, comparisons against named variables (never raw numbers repeated inline). The FIRST function must be the program's main loop body (what runs every cycle inside the outermost repeat/forever block); every function after it is a separate reusable helper the main loop calls by name. Give each function a short `usage_note`: one sentence of tuning advice for a student adapting this to their own robot, or "" if there's nothing worth adding.

Example of the pseudocode STYLE expected in `body` (do not copy this content, only the style):
    if IR3 < IR_value and IR4 < IR_value:
      add 1 to junction_count
      if junction_count == 1: call turn_right
      else: call cross_forward
    elif IR3 > IR_value and IR4 < IR_value:
      call turn_right
    else:
      call forward

This is a DRAFT a human coach will review and edit before anything is saved -- get the structure and logic right; exact wording matters less."""


def _summarize_stored_blocks(blocks: list[dict]) -> str:
    """Same idea as pipeline.summarize_blocks, but over the raw dicts a
    session turn stores (already-serialized JSON, not ProgramBlock
    instances) — so a later phase's history shows what an earlier phase
    already placed."""
    names: list[str] = []

    def walk(block_list: list[dict]) -> None:
        for b in block_list:
            names.append(b.get("name", "?"))
            walk(b.get("conditions") or [])
            walk(b.get("children") or [])
            walk(b.get("else_children") or [])

    walk(blocks)
    return ", ".join(names) if names else "(none)"


def _conversation_history(session: Optional[Session], mode: str) -> Optional[list[dict]]:
    """Prior turns of an OPEN Build thread (an `ask` or `phase` awaiting the
    student's reply), oldest first — or None to start fresh.

    Build-only by construction — Debug has its own, differently-shaped
    version (`_debug_conversation_history`, DebugAnswer's simpler two-kind
    schema uses `summary` where Build uses `message`, and has no phase
    concept). A Build thread is "open" only when the session's LAST turn is
    an assistant turn with kind in (ask, phase) — a `complete` turn closes
    it, and the next Build question starts over.
    """
    if mode != "build" or session is None or not session.turns:
        return None

    last = session.turns[-1]
    if last.get("role") != "assistant" or last.get("mode") != "build":
        return None
    if last.get("answer", {}).get("kind") not in ("ask", "phase"):
        return None

    # Walk backward, collecting this thread's turns. Stop at (and exclude)
    # the boundary of the PREVIOUS thread: a non-build turn, or a build
    # assistant turn with kind == "complete".
    open_turns: list[dict] = []
    for turn in reversed(session.turns):
        if turn.get("mode") != "build":
            break
        if turn.get("role") == "assistant" and turn.get("answer", {}).get("kind") == "complete":
            break
        open_turns.append(turn)
    open_turns.reverse()

    history: list[dict] = []
    for turn in open_turns:
        if turn.get("role") == "student":
            history.append({"role": "user", "content": turn.get("question", "")})
        else:
            turn_answer = turn.get("answer", {})
            content = turn_answer.get("message", "")
            blocks = turn_answer.get("blocks") or []
            if blocks:
                content += "\nBlocks so far: " + _summarize_stored_blocks(blocks)
            history.append({"role": "assistant", "content": content})
    return history


def _debug_conversation_history(session: Optional[Session]) -> Optional[list[dict]]:
    """Prior turns of an OPEN Debug thread -- the one clarifying question a
    Debug turn is allowed to ask (DebugAnswer.kind, app/llm/pipeline.py's
    _DEBUG_EXTRA) awaiting the student's reply. Mirrors
    `_conversation_history`'s own shape, adapted to Debug's simpler
    two-kind schema: `ask` (the question sits in `summary`, not `message`
    -- Debug has no such field) keeps the thread open, `answer` closes it,
    same reasoning as Build's `complete` closing its own thread, so the
    NEXT Debug question starts fresh rather than dragging in stale context
    from a problem already diagnosed."""
    if session is None or not session.turns:
        return None

    last = session.turns[-1]
    if last.get("role") != "assistant" or last.get("mode") != "debug":
        return None
    if last.get("answer", {}).get("kind") != "ask":
        return None

    open_turns: list[dict] = []
    for turn in reversed(session.turns):
        if turn.get("mode") != "debug":
            break
        if turn.get("role") == "assistant" and turn.get("answer", {}).get("kind") == "answer":
            break
        open_turns.append(turn)
    open_turns.reverse()

    history: list[dict] = []
    for turn in open_turns:
        if turn.get("role") == "student":
            history.append({"role": "user", "content": turn.get("question", "")})
        else:
            history.append({"role": "assistant", "content": turn.get("answer", {}).get("summary", "")})
    return history


def _is_if_then_wrapper(block: dict) -> bool:
    return block.get("name") == "if then" and bool(block.get("children") or block.get("else_children"))


def _format_block_line(b: dict, pad: str) -> str:
    name = b.get("name", "?")
    slot_text = " ".join(f"[{s.get('text', '')}]" for s in (b.get("slots") or []))
    conditions = b.get("conditions") or []
    cond_text = ""
    if conditions:
        parts = []
        for c in conditions:
            c_slots = " ".join(f"[{s.get('text', '')}]" for s in (c.get("slots") or []))
            parts.append(f"{c.get('name', '?')} {c_slots}".strip())
        cond_text = " condition=(" + "; ".join(parts) + ")"
    return f"{pad}{name} {slot_text}{cond_text}".rstrip()


def _render_blocks_as_text(blocks: list[dict], indent: int = 0) -> list[str]:
    """The full completed program, as readable structured text (block
    names, EVERY slot's real value, and real nesting via `children`/
    `else_children`) -- unlike `_summarize_stored_blocks`'s flat
    comma-separated names (enough to show phase-by-phase progress, not
    enough to correctly PLACE a new addition into an already-finished
    program). Used only by `_continuation_history_after_complete`, where
    the model needs to know exactly what it already built, not just that
    it built something.

    Flattens an if/elif/.../else chain to the SAME indentation for every
    branch, matching CBlock's own chain-fusing logic (index.html) that the
    real student app already renders with. Caught live: an earlier version
    of this function indented one level deeper per `else_children` --
    technically true to the raw JSON (R Block's own containment shape
    nests each elif one level into the previous one's else_children), but
    reads as increasingly nested code instead of a normal flat
    if/elif/elif/else, exactly what a coach flagged as hard to read."""
    lines: list[str] = []
    for b in blocks:
        pad = "  " * indent
        chain = [b]
        while True:
            else_children = chain[-1].get("else_children") or []
            if len(else_children) == 1 and _is_if_then_wrapper(else_children[0]):
                chain.append(else_children[0])
            else:
                break
        tail_else = chain[-1].get("else_children") or []

        for seg in chain:
            lines.append(_format_block_line(seg, pad))
            lines.extend(_render_blocks_as_text(seg.get("children") or [], indent + 1))
        if tail_else:
            lines.append(f"{pad}else:")
            lines.extend(_render_blocks_as_text(tail_else, indent + 1))
    return lines


def _continuation_history_after_complete(session: Optional[Session]) -> Optional[list[dict]]:
    """For a follow-up from the ANSWER SCREEN (AskRequest.continue_thread)
    after a `kind=complete` turn -- a different case from
    `_conversation_history`'s multi-phase thread walk: there is no
    in-progress thread to resume, just ONE finished program the student
    now wants adjusted ("now add end program at count 3"). Returns a
    single synthetic assistant turn carrying that program's real block
    detail, or None if the session's last turn isn't actually a completed
    build -- the caller falls back to `_conversation_history` in that case,
    which already handles an open ask/phase thread correctly.

    Caught live: a student got a real, correct 15-block program (kind=
    complete), then asked to add one more thing to it -- the model had no
    way to see what it had just built (a `complete` turn closes the thread
    on purpose, so the next question started from nothing) and could only
    ask "which program?". `continue_thread` exists specifically so the
    reply box already shown under a finished answer ("ask a follow-up if
    you want the AI to adjust it") means what it says."""
    if session is None or not session.turns:
        return None
    last = session.turns[-1]
    if last.get("role") != "assistant" or last.get("mode") != "build":
        return None
    answer = last.get("answer") or {}
    if answer.get("kind") != "complete":
        return None
    blocks = answer.get("blocks") or []
    content = answer.get("message", "")
    if blocks:
        content += "\n\nThe program as it currently stands:\n" + "\n".join(_render_blocks_as_text(blocks))
    # Caught live: returning ONLY this assistant turn means the real request
    # sent to the model is system -> assistant -> user (the follow-up),
    # with no user turn ever preceding that assistant one -- every other
    # history path in this codebase alternates user/assistant properly.
    # Observed failure: the model treated the bare assistant turn as its
    # own message to keep writing, and replied in prose ("Phase 3: ...")
    # instead of a fresh JSON answer, tripping the "model did not return
    # valid JSON" error on a request that should have succeeded. A
    # synthetic user turn ahead of it restores normal alternation.
    return [
        {"role": "user", "content": "Show me the program you just built."},
        {"role": "assistant", "content": content},
    ]


class NoCacheStaticFiles(StaticFiles):
    """Every static asset (index.html, admin.html, admin_skills.html, ...)
    served with `Cache-Control: no-cache` instead of Starlette's default of
    no explicit header at all -- caught live, repeatedly, this session: a
    browser's own HEURISTIC freshness lifetime (RFC 7234, ~10% of the
    file's age since it was last modified) meant an edited admin.html kept
    getting served from cache with no visible way to tell it was stale,
    needing a manual hard-refresh every single time, on both admin AND
    student devices. `no-cache` (not `no-store`) still lets the browser
    keep a local copy -- it just forces a fast conditional revalidation
    (If-None-Match/If-Modified-Since, a cheap 304 if unchanged) before
    ever using it, so a real edit is picked up on the very next load with
    no stale "stacked" version ever served, at the cost of one small round
    trip per load instead of a silent, possibly-very-long, cache hit."""

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


def _default_restart() -> None:
    """Re-executes this SAME process in place (same PID, same Terminal
    window/session) rather than spawning a detached child -- deliberate,
    matching the "server closes when the terminal closes" setup chosen
    earlier: a spawned child would outlive the terminal, silently
    reintroducing the always-on background server that setup was meant to
    avoid.

    `sys.orig_argv`, NOT `sys.argv` -- caught live, this crashed the real
    server the first time it ran: launched as `python3 -m uvicorn
    app.api.main:app ...`, `sys.argv[0]` has already been rewritten by
    Python's own `-m` handling to the resolved file path of uvicorn's
    `__main__.py`, so `[sys.executable] + sys.argv` re-execs as `python3
    /path/to/uvicorn/__main__.py ...` -- running that file DIRECTLY rather
    than via `-m` puts its own directory at the front of sys.path, which
    shadowed the real top-level `uvicorn` package and broke on a circular
    `import uvicorn` inside its own `__main__.py`, with a bare
    AttributeError several frames deep into stdlib `logging` and no hint
    of the real cause. `sys.orig_argv` (Python 3.10+) is the interpreter
    command line exactly as it was actually typed/launched, `-m` and all
    -- verified live (a two-phase re-exec test app) to restart cleanly
    where the naive `sys.argv` version did not."""
    os.execv(sys.executable, sys.orig_argv)


def _client_for_backend(backend: str) -> ChatClient:
    if backend == "gemini":
        return GeminiClient()
    if backend == "openrouter":
        return OpenRouterClient()
    return OllamaClient()


def create_app(
    client: Optional[ChatClient] = None,
    queue: Optional[QueueManager] = None,
    sessions: Optional[FileSessionStore] = None,
    log: Optional[LogWriter] = None,
    admin_auth: Optional[AdminAuth] = None,
    site_auth: Optional[SiteAuth] = None,
    env_path: Optional[Path] = None,
    embedder: Optional[OllamaEmbedder] = None,
    skills_path: Optional[Path] = None,
    prompts_path: Optional[Path] = None,
    levels_path: Optional[Path] = None,
    directory_path: Optional[Path] = None,
    tts_client: Optional[ElevenLabsTTS] = None,
    # Injectable so tests can verify /admin/restart's auth/queue-check logic
    # without actually re-executing the test process -- see _default_restart.
    restart_fn: Optional[Callable[[], None]] = None,
) -> FastAPI:
    env_path = env_path or (config.REPO_ROOT / ".env")
    client = client or _client_for_backend(config.MODEL_BACKEND)
    queue = queue or QueueManager(
        concurrency=config.QUEUE_CONCURRENCY,
        max_queue_depth=config.QUEUE_MAX_DEPTH,
        min_wait_seconds=config.QUEUE_MIN_WAIT_SECONDS,
        max_wait_seconds=config.QUEUE_MAX_WAIT_SECONDS,
    )
    sessions = sessions or FileSessionStore()
    log = log or LogWriter()
    # A live callable, not the frozen `config.ADMIN_PASSWORD` constant --
    # see AdminAuth.password and config.current_admin_password for why:
    # editing .env's ADMIN_PASSWORD line while the server is running now
    # takes effect on the next login attempt instead of needing a restart.
    admin_auth = admin_auth or AdminAuth(password=config.current_admin_password)
    # Same live-reload reasoning as admin_auth above -- gates the whole
    # student-facing app, not just /admin/*, for once it's reachable over
    # the public internet via a Cloudflare Tunnel rather than just the
    # school's own wifi (see app/site_auth.py's module docstring).
    #
    # Bound to env_path, NOT the process-global config.current_site_password
    # -- same reasoning as admin_settings' own "seeded from env_path
    # directly" note below. Caught immediately after adding SITE_PASSWORD to
    # the real .env: every test that hits any endpoint started 401ing,
    # because create_app was reading the developer's real .env rather than
    # the isolated tmp_path one each test passes in. An isolated env_path
    # with no SITE_PASSWORD now correctly means "gate off", while the real
    # deployment (env_path IS the real .env) is unchanged.
    site_auth = site_auth or SiteAuth(password=lambda: config.current_site_password(env_path))
    embedder = embedder or OllamaEmbedder()
    skills_path = skills_path or skills_store.DEFAULT_PATH
    prompts_path = prompts_path or prompts_store.DEFAULT_PATH
    levels_path = levels_path or levels_store.DEFAULT_PATH
    directory_path = directory_path or levels_store.DIRECTORY_PATH
    restart_fn = restart_fn or _default_restart
    tts_client = tts_client or ElevenLabsTTS(
        api_key=config.ELEVENLABS_API_KEY,
        voice_id=config.ELEVENLABS_VOICE_ID,
        model_id=config.ELEVENLABS_TTS_MODEL,
        timeout_seconds=config.ELEVENLABS_TIMEOUT_SECONDS,
    )
    # Runtime view of settings an admin can change — separate from the
    # config.* module constants (those are only the values at process
    # start). /admin/settings reads and writes this; the pipeline never
    # touches it directly, only app.state.client.
    #
    # Seeded from env_path directly (not config.* globals): config.* reflect
    # whatever .env this PROCESS happened to start with, which is not
    # necessarily env_path (e.g. a test's isolated tmp_path file) — reading
    # the same file the admin endpoints write to keeps them consistent.
    env_file_values = config.read_dotenv_file(env_path)
    admin_settings = {
        "backend": env_file_values.get("MODEL_BACKEND", config.MODEL_BACKEND),
        "ollama_model": env_file_values.get("OLLAMA_MODEL", config.OLLAMA_MODEL),
        "gemini_model": env_file_values.get("GEMINI_MODEL", config.GEMINI_MODEL),
        "openrouter_model": env_file_values.get("OPENROUTER_MODEL", config.OPENROUTER_MODEL),
        # No config.* fallback here specifically: whether a key is "set" must
        # answer from env_path alone, or an isolated test env_path (or any
        # deployment where env_path differs from whatever .env this process
        # happened to start with) would report a leaked key that isn't
        # actually in the file being read and written.
        "gemini_api_key_set": bool(env_file_values.get("GEMINI_API_KEY")),
        "openrouter_api_key_set": bool(env_file_values.get("OPENROUTER_API_KEY")),
        "elevenlabs_api_key_set": bool(env_file_values.get("ELEVENLABS_API_KEY")),
        "elevenlabs_voice_id": env_file_values.get("ELEVENLABS_VOICE_ID", config.ELEVENLABS_VOICE_ID),
    }

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        queue.start()
        if hasattr(client, "warmup"):
            try:
                await client.warmup()
                app.state.model_warm = True
            except Exception:
                # Ollama may still be starting up; the first real question
                # will load the model instead. Don't block server startup
                # on it, and don't crash the app over a warm-up failure.
                # model_warm stays False -- the entry screen's badge (Q:
                # "give user message on model ready") will keep showing
                # "warming up" until a request actually succeeds.
                pass
        yield
        await queue.stop()

    app = FastAPI(title="R Block Coding Buddy", lifespan=lifespan)
    app.state.client = client
    app.state.queue = queue
    app.state.sessions = sessions
    app.state.log = log
    app.state.admin_auth = admin_auth
    app.state.site_auth = site_auth
    app.state.admin_settings = admin_settings
    app.state.embedder = embedder
    app.state.tts_client = tts_client
    app.state.skills_path = skills_path
    app.state.restart_fn = restart_fn
    # A cloud backend (Gemini) has no local model to warm up, so it's
    # "ready" immediately; Ollama starts False until lifespan's warmup()
    # above succeeds (or, as a fallback, until any real request completes
    # -- see the /ask handler).
    app.state.model_warm = not hasattr(client, "warmup")
    # Loaded once at startup and refreshed on every admin CRUD write
    # (in-process cache, not read from disk per request) — a handful of
    # documents, edited rarely, matching admin_settings' own pattern above.
    app.state.skills = skills_store.load_all(skills_path)
    app.state.prompts_path = prompts_path
    # Same in-process-cache-refreshed-on-write pattern as skills above --
    # the /ask handler reads this on EVERY call (app/llm/pipeline.py's
    # `answer()` takes it as `prompts=`), so an admin's save takes effect
    # on the very next request, no restart needed.
    app.state.prompts = prompts_store.load(prompts_path)
    # Same in-process-cache-refreshed-on-write pattern as skills/prompts.
    app.state.levels_path = levels_path
    app.state.levels = levels_store.load_all(levels_path)
    app.state.directory_path = directory_path
    app.state.directory = levels_store.load_directory(directory_path)

    def _require_admin(request: Request) -> None:
        auth_header = request.headers.get("Authorization", "")
        token = auth_header.removeprefix("Bearer ").strip() if auth_header.startswith("Bearer ") else None
        if not request.app.state.admin_auth.check(token):
            raise HTTPException(status_code=401, detail="admin login required")

    @app.get("/health")
    async def health(request: Request) -> dict:
        state = request.app.state
        return {
            "status": "ok",
            "backend": state.admin_settings["backend"],
            "model": getattr(state.client, "model", None),
            "warm": state.model_warm,
            # Answers "is this actually running today's edit?" -- see
            # config.py's STARTED_AT/CODE_FINGERPRINT docstring. started_at
            # is Unix seconds (not a human string) so it's trivial to diff
            # against "now" from the command line with no parsing.
            "started_at": config.STARTED_AT,
            "code_fingerprint": config.CODE_FINGERPRINT,
            "tts_available": bool(state.tts_client.api_key),
        }

    @app.post("/tts")
    async def text_to_speech(payload: TTSRequest, request: Request) -> Response:
        """Voice for the assistant's own non-code turns (a clarifying
        question, a plan-confirmation message) -- never for a phase/complete
        turn's blocks, which the frontend never sends here (see index.html's
        `speakIfEnabled`, only called for `kind === "ask"`). Public, no admin
        auth, same as /ask -- this is a normal part of a student's turn, not
        an admin function. 503 (not a 500) when no key is configured, so the
        frontend can treat "TTS not set up" as a quiet no-op rather than a
        real error."""
        state = request.app.state
        try:
            result = await state.tts_client.synthesize(payload.text)
        except ModelUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return Response(content=result.audio_bytes, media_type=result.content_type)

    @app.post("/admin/login", response_model=AdminLoginResponse)
    async def admin_login(payload: AdminLoginRequest, request: Request) -> AdminLoginResponse:
        token = request.app.state.admin_auth.login(payload.password)
        if token is None:
            raise HTTPException(status_code=401, detail="wrong password, or admin login is not configured")
        return AdminLoginResponse(token=token)

    @app.get("/admin/settings", response_model=AdminSettingsResponse)
    async def get_admin_settings(request: Request) -> AdminSettingsResponse:
        _require_admin(request)
        s = request.app.state.admin_settings
        return AdminSettingsResponse(
            backend=s["backend"],
            ollama_model=s["ollama_model"],
            ollama_host=config.OLLAMA_HOST,
            gemini_model=s["gemini_model"],
            gemini_api_key_set=s["gemini_api_key_set"],
            openrouter_model=s["openrouter_model"],
            openrouter_api_key_set=s["openrouter_api_key_set"],
            elevenlabs_api_key_set=s["elevenlabs_api_key_set"],
            elevenlabs_voice_id=s["elevenlabs_voice_id"],
        )

    @app.post("/admin/settings", response_model=AdminSettingsResponse)
    async def update_admin_settings(payload: AdminSettingsUpdate, request: Request) -> AdminSettingsResponse:
        _require_admin(request)
        state = request.app.state
        s = state.admin_settings

        new_ollama_model = payload.ollama_model or s["ollama_model"]
        new_gemini_model = payload.gemini_model or s["gemini_model"]
        new_gemini_key_set = s["gemini_api_key_set"] or bool(payload.gemini_api_key)
        new_openrouter_model = payload.openrouter_model or s["openrouter_model"]
        new_openrouter_key_set = s["openrouter_api_key_set"] or bool(payload.openrouter_api_key)
        new_elevenlabs_key_set = s["elevenlabs_api_key_set"] or bool(payload.elevenlabs_api_key)
        new_elevenlabs_voice_id = payload.elevenlabs_voice_id or s["elevenlabs_voice_id"]

        if payload.backend == "gemini" and not new_gemini_key_set:
            raise HTTPException(
                status_code=400,
                detail="cannot switch to gemini without a Gemini API key — set one first",
            )
        if payload.backend == "openrouter" and not new_openrouter_key_set:
            raise HTTPException(
                status_code=400,
                detail="cannot switch to openrouter without an OpenRouter API key — set one first",
            )

        if payload.ollama_model and payload.backend == "local":
            # Best-effort: don't let a typo silently break the app. If
            # Ollama itself can't be reached right now, don't block the
            # save over it -- the admin may be configuring ahead of time.
            try:
                async with httpx.AsyncClient(timeout=5) as http_client:
                    resp = await http_client.get(f"{config.OLLAMA_HOST}/api/tags")
                    resp.raise_for_status()
                    available = {m["name"] for m in resp.json().get("models", [])}
                    if available and new_ollama_model not in available:
                        raise HTTPException(
                            status_code=400,
                            detail=f"'{new_ollama_model}' is not pulled in Ollama yet (run: ollama pull {new_ollama_model})",
                        )
            except httpx.HTTPError:
                pass

        env_updates = {"MODEL_BACKEND": payload.backend}
        if payload.ollama_model:
            env_updates["OLLAMA_MODEL"] = payload.ollama_model
        if payload.gemini_model:
            env_updates["GEMINI_MODEL"] = payload.gemini_model
        if payload.gemini_api_key:
            env_updates["GEMINI_API_KEY"] = payload.gemini_api_key
        if payload.openrouter_model:
            env_updates["OPENROUTER_MODEL"] = payload.openrouter_model
        if payload.openrouter_api_key:
            env_updates["OPENROUTER_API_KEY"] = payload.openrouter_api_key
        if payload.elevenlabs_api_key:
            env_updates["ELEVENLABS_API_KEY"] = payload.elevenlabs_api_key
        if payload.elevenlabs_voice_id:
            env_updates["ELEVENLABS_VOICE_ID"] = payload.elevenlabs_voice_id
        config.write_dotenv_updates(env_updates, path=env_path)

        s["backend"] = payload.backend
        s["ollama_model"] = new_ollama_model
        s["gemini_model"] = new_gemini_model
        s["gemini_api_key_set"] = new_gemini_key_set
        s["openrouter_model"] = new_openrouter_model
        s["openrouter_api_key_set"] = new_openrouter_key_set
        s["elevenlabs_api_key_set"] = new_elevenlabs_key_set
        s["elevenlabs_voice_id"] = new_elevenlabs_voice_id

        # Hot-swap, independent of the backend branch below (TTS has no
        # "which backend" concept — see AdminSettingsResponse's comment).
        # Either a new key OR just a new voice should take effect
        # immediately, not only when the key itself changes.
        if payload.elevenlabs_api_key or payload.elevenlabs_voice_id:
            state.tts_client = ElevenLabsTTS(
                api_key=payload.elevenlabs_api_key or state.tts_client.api_key,
                voice_id=new_elevenlabs_voice_id,
                model_id=state.tts_client.model_id,
                timeout_seconds=state.tts_client.timeout_seconds,
            )

        # Hot-swap: takes effect on the very next /ask, no restart needed.
        if payload.backend == "gemini":
            new_client = GeminiClient(
                api_key=payload.gemini_api_key or config.GEMINI_API_KEY, model=new_gemini_model
            )
            state.model_warm = True  # no local model to warm up
        elif payload.backend == "openrouter":
            new_client = OpenRouterClient(
                api_key=payload.openrouter_api_key or config.OPENROUTER_API_KEY, model=new_openrouter_model
            )
            state.model_warm = True  # no local model to warm up
        else:
            new_client = OllamaClient(model=new_ollama_model)
            state.model_warm = False  # a newly swapped-in local model isn't loaded yet
            if hasattr(new_client, "warmup"):
                try:
                    await new_client.warmup()
                    state.model_warm = True
                except Exception:
                    pass  # same reasoning as startup warm-up: don't block on it
        state.client = new_client

        return AdminSettingsResponse(
            backend=s["backend"],
            ollama_model=s["ollama_model"],
            ollama_host=config.OLLAMA_HOST,
            gemini_model=s["gemini_model"],
            gemini_api_key_set=s["gemini_api_key_set"],
            openrouter_model=s["openrouter_model"],
            openrouter_api_key_set=s["openrouter_api_key_set"],
            elevenlabs_api_key_set=s["elevenlabs_api_key_set"],
            elevenlabs_voice_id=s["elevenlabs_voice_id"],
        )

    _PROMPT_FIELDS = ("system_template", "build_extra", "debug_extra")
    _PROMPT_HARDCODED_DEFAULTS = {
        "system_template": lambda: pipeline_module.DEFAULT_SYSTEM_TEMPLATE,
        "build_extra": lambda: pipeline_module.DEFAULT_BUILD_EXTRA,
        "debug_extra": lambda: pipeline_module.DEFAULT_DEBUG_EXTRA,
    }

    def _prompts_response(overrides: PromptOverrides) -> PromptsResponse:
        kwargs = {}
        for field in _PROMPT_FIELDS:
            hardcoded = _PROMPT_HARDCODED_DEFAULTS[field]()
            effective_default = overrides.effective_default(field, hardcoded)
            kwargs[field] = overrides.effective(field, hardcoded)
            kwargs[f"{field}_is_default"] = getattr(overrides, field) is None
            kwargs[f"default_{field}"] = effective_default
            kwargs[f"default_{field}_is_custom"] = getattr(overrides, f"{field}_default") is not None
        return PromptsResponse(**kwargs)

    @app.get("/admin/prompts", response_model=PromptsResponse)
    async def get_admin_prompts(request: Request) -> PromptsResponse:
        _require_admin(request)
        return _prompts_response(request.app.state.prompts)

    @app.post("/admin/prompts", response_model=PromptsResponse)
    async def update_admin_prompts(payload: PromptsUpdate, request: Request) -> PromptsResponse:
        """Full-replace, guarded: an admin can rewrite any of these three
        prompt pieces entirely (not just append tone guidance), but nothing
        gets saved without first passing validate_prompt_override -- a
        broken template would otherwise fail silently on the very next
        student request, not here where there's still a chance to fix it.
        Applies immediately (state.prompts, read by every /ask call), same
        "no restart needed" pattern as /admin/settings' hot-swapped client.

        Two independent actions per field, both optional, both go through
        the SAME validation: setting the active text (or "" to reset it to
        this field's current default), and "set as default" (promotes text
        into `<field>_default`, PromptOverrides' second layer -- see its
        own docstring for why "reset" needs a second layer at all).
        """
        _require_admin(request)
        state = request.app.state
        overrides = state.prompts

        for field in _PROMPT_FIELDS:
            if getattr(payload, f"{field}_restore_original"):
                # Wins over everything else for this field -- clears BOTH
                # layers, no validation needed (None is always valid).
                setattr(overrides, field, None)
                setattr(overrides, f"{field}_default", None)
                continue

            value = getattr(payload, field)
            set_as_default = getattr(payload, f"{field}_set_as_default")

            if value is not None:
                value = value.strip()
                if not value:
                    setattr(overrides, field, None)  # explicit empty string = reset to default
                else:
                    error = pipeline_module.validate_prompt_override(field, value)
                    if error:
                        raise HTTPException(status_code=400, detail=f"{field}: {error}")
                    setattr(overrides, field, value)

            if set_as_default:
                # Promotes whatever text is ACTIVE for this field right now
                # (just-applied above if `value` was sent this same
                # request, otherwise whatever was already active) into the
                # new default. Already valid -- it's either the
                # just-validated `value`, or text that passed validation on
                # a previous save -- but re-validated anyway rather than
                # trusting that invariant across a save this module doesn't
                # fully control the shape of.
                hardcoded = _PROMPT_HARDCODED_DEFAULTS[field]()
                new_default = overrides.effective(field, hardcoded)
                error = pipeline_module.validate_prompt_override(field, new_default)
                if error:
                    raise HTTPException(status_code=400, detail=f"{field}: {error}")
                setattr(overrides, f"{field}_default", new_default)
                setattr(overrides, field, None)  # now IS the default -- no active override needed

        prompts_store.save(overrides, state.prompts_path)
        state.prompts = overrides
        return _prompts_response(overrides)

    @app.get("/levels", response_model=list[LevelOut])
    async def get_levels(request: Request) -> list[LevelOut]:
        """Names only -- the prompts stay admin-side. Behind the site
        gate like everything else student-facing."""
        return [LevelOut(id=lv.id, name=lv.name) for lv in request.app.state.levels]

    @app.get("/admin/levels", response_model=list[LevelAdminOut])
    async def get_admin_levels(request: Request) -> list[LevelAdminOut]:
        _require_admin(request)
        return [LevelAdminOut(**lv.to_dict()) for lv in request.app.state.levels]

    @app.put("/admin/levels", response_model=list[LevelAdminOut])
    async def put_admin_levels(payload: list[LevelAdminIn], request: Request) -> list[LevelAdminOut]:
        """Full replace of the list: add = a new entry, set = an edited
        entry, remove = an entry left out. Entries without an id get one."""
        _require_admin(request)
        state = request.app.state
        known_keys = {e["key"] for e in state.directory}
        levels: list[levels_store.Level] = []
        seen_names: set[str] = set()
        for item in payload:
            name = item.name.strip()
            if not name:
                raise HTTPException(status_code=400, detail="every level needs a name")
            if name.lower() in seen_names:
                raise HTTPException(status_code=400, detail=f"duplicate level name: {name}")
            seen_names.add(name.lower())
            levels.append(
                levels_store.Level(
                    id=item.id or levels_store.new_id(name),
                    name=name,
                    prompt=item.prompt.strip(),
                    directory_keys=[k for k in dict.fromkeys(item.directory_keys) if k in known_keys],
                )
            )
        levels_store.save_all(levels, state.levels_path)
        state.levels = levels
        return [LevelAdminOut(**lv.to_dict()) for lv in levels]

    @app.get("/admin/directory-context", response_model=list[DirectoryEntryIn])
    async def get_directory_context(request: Request) -> list[DirectoryEntryIn]:
        _require_admin(request)
        return [DirectoryEntryIn(**e) for e in request.app.state.directory]

    @app.put("/admin/directory-context", response_model=list[DirectoryEntryIn])
    async def put_directory_context(payload: list[DirectoryEntryIn], request: Request) -> list[DirectoryEntryIn]:
        """Full replace of the key/value entries a level can pick from.
        Levels pointing at a key that no longer exists are pruned."""
        _require_admin(request)
        state = request.app.state
        entries: list[dict] = []
        seen: set[str] = set()
        for item in payload:
            key = item.key.strip()
            if not key:
                raise HTTPException(status_code=400, detail="every directory entry needs a key")
            if key in seen:
                raise HTTPException(status_code=400, detail=f"duplicate directory key: {key}")
            seen.add(key)
            entries.append({"key": key, "value": item.value.strip()})
        levels_store.save_directory(entries, state.directory_path)
        state.directory = entries
        for lv in state.levels:
            lv.directory_keys = [k for k in lv.directory_keys if k in seen]
        levels_store.save_all(state.levels, state.levels_path)
        return [DirectoryEntryIn(**e) for e in entries]

    def _replace_directory(state, entries: list[dict]) -> None:
        levels_store.save_directory(entries, state.directory_path)
        state.directory = entries
        keys = {e["key"] for e in entries}
        for lv in state.levels:
            lv.directory_keys = [k for k in lv.directory_keys if k in keys]
        levels_store.save_all(state.levels, state.levels_path)

    @app.post("/admin/directory-context/set-default", response_model=DirectoryActionOut)
    async def set_directory_default(request: Request) -> DirectoryActionOut:
        """Promotes the SAVED entries to be the default copy, i.e. rewrites
        vendor/r-block-directory.md (validated first; the startup checksum
        is updated to match). The page saves its edits before calling this.
        The block-name rules are built at import, so a restart applies it."""
        _require_admin(request)
        state = request.app.state
        try:
            block_directory_module.write_default(state.directory)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return DirectoryActionOut(
            entries=[DirectoryEntryIn(**e) for e in state.directory],
            note="Saved as the default copy (r-block-directory.md). Restart the server to apply it to the block-name rules.",
        )

    @app.post("/admin/directory-context/recover", response_model=DirectoryActionOut)
    async def recover_directory_default(request: Request) -> DirectoryActionOut:
        """Working entries back to whatever the default copy currently is."""
        _require_admin(request)
        state = request.app.state
        _replace_directory(state, block_directory_module.read_default_entries())
        return DirectoryActionOut(
            entries=[DirectoryEntryIn(**e) for e in state.directory],
            note="Entries reset to the default copy.",
        )

    @app.post("/admin/directory-context/factory-reset", response_model=DirectoryActionOut)
    async def factory_reset_directory(request: Request) -> DirectoryActionOut:
        """Default copy AND working entries back to the original shipped
        directory (vendor/r-block-directory.factory.md)."""
        _require_admin(request)
        state = request.app.state
        try:
            block_directory_module.restore_factory()
            entries = block_directory_module.read_factory_entries()
        except RuntimeError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        _replace_directory(state, entries)
        return DirectoryActionOut(
            entries=[DirectoryEntryIn(**e) for e in entries],
            note="Reset to factory. Restart the server if you had set a different default before, so the block-name rules match.",
        )

    @app.post("/admin/restart", response_model=AdminRestartResponse)
    async def restart_server(payload: AdminRestartRequest, request: Request) -> AdminRestartResponse:
        """Re-execs this process in place (see _default_restart) -- the
        remote equivalent of what an admin would otherwise do by hand in
        the Terminal window running the server. Picks up a code change or
        a hand-edited .env; does NOT survive if the ORIGINAL launch command
        itself was broken (it just repeats that same command).

        Any student mid-request when this fires is dropped with no graceful
        drain -- same as Ctrl+C'ing the terminal today. force=False (the
        default) surfaces that as a 409 with the real count instead of
        restarting blind, so the admin panel can show a coach exactly what
        they're about to interrupt before they confirm."""
        _require_admin(request)
        state = request.app.state
        active_jobs = state.queue.active_job_count()
        if active_jobs and not payload.force:
            raise HTTPException(
                status_code=409,
                detail=f"{active_jobs} student request(s) still in progress -- retry with force to restart anyway",
            )

        async def _delayed_restart() -> None:
            # Long enough for this response to actually reach the browser
            # before the process disappears -- a restart that raced its own
            # HTTP response would look like a network failure, not a
            # confirmed restart.
            await asyncio.sleep(0.5)
            state.restart_fn()

        asyncio.create_task(_delayed_restart())
        return AdminRestartResponse(restarting=True, active_jobs=active_jobs)

    @app.get("/admin/skills", response_model=list[SkillDocOut])
    async def list_skills(request: Request) -> list[SkillDocOut]:
        _require_admin(request)
        return [_skill_to_out(s) for s in request.app.state.skills]

    @app.post("/admin/skills", response_model=SkillDocOut)
    async def save_skill(payload: SkillDocIn, request: Request) -> SkillDocOut:
        _require_admin(request)
        state = request.app.state
        doc = _skill_from_payload(payload)
        # Best-effort, same reasoning as the Ollama model check above: a save
        # while the embedding backend happens to be down still saves (with
        # an empty embedding, degrading that skill to keyword-only matching
        # until the next successful save recomputes one), rather than
        # blocking the teacher's edit on it.
        doc.embedding = await compute_embedding(doc, state.embedder)
        try:
            # The one AI call in the whole save path: renders this skill's
            # reference program into real R Block JSON ONCE now, so Economy
            # mode never needs to call the model again for it (see
            # app/skills/demo.py and app/llm/economy.py's own comments).
            # Best-effort, same reasoning as compute_embedding above -- a
            # save with the model backend down still saves (Economy mode
            # falls back to plain pseudocode text for this skill until the
            # next successful save), rather than blocking the coach's edit.
            doc.demo_render = await render_demo_blocks(doc, state.client)
        except ModelUnavailable:
            doc.demo_render = []
        skills_store.upsert(doc, path=state.skills_path)
        state.skills = skills_store.load_all(state.skills_path)
        saved = skills_store.get(doc.id, state.skills_path)
        out = _skill_to_out(saved)
        # Render it back through the SAME two functions the live paths call
        # (not a re-implementation) so what the admin sees after saving is
        # exactly what a real turn would produce -- the AI's own context and
        # the Economy-mode student view, side by side.
        economy = build_economy_answer(saved)
        out.preview = SkillPreviewOut(
            ai_context=render_skill(saved),
            student_message=economy.message,
            student_code=economy.code_text,
            student_render=economy.render,
        )
        return out

    @app.delete("/admin/skills/{skill_id}")
    async def delete_skill(skill_id: str, request: Request) -> dict:
        _require_admin(request)
        state = request.app.state
        if not skills_store.delete(skill_id, path=state.skills_path):
            raise HTTPException(status_code=404, detail="unknown skill id")
        state.skills = skills_store.load_all(state.skills_path)
        return {"deleted": skill_id}

    @app.post("/admin/skills/draft-from-image", response_model=SkillDraftOut)
    async def draft_skill_from_image(payload: SkillDraftRequest, request: Request) -> SkillDraftOut:
        """A complement to the manual editor: a coach attaches a photo of a
        reference program, the AI writes a first-pass skill doc from it, and
        the coach reviews/edits before ever saving -- this endpoint never
        touches the skills store itself, it only returns a draft."""
        _require_admin(request)
        state = request.app.state
        system = _SKILL_DRAFT_SYSTEM_PROMPT
        note = payload.note.strip()
        if note:
            system += f"\n\nThe coach also wrote this note about the photo: {note}"
        schema = SkillDraftOut.model_json_schema()
        user = "Read the attached photo of a reference block program and draft the skill doc."

        # Same "one retry, only for unparseable output" policy as the main
        # pipeline (app/llm/pipeline.py's answer()) -- a transient provider
        # hiccup is worth one bare retry; a real shape problem is not.
        try:
            for attempt in range(2):
                result = await state.client.chat(
                    system=system, user=user, schema=schema, image_base64=payload.image_base64
                )
                try:
                    raw = json.loads(result.text)
                    break
                except json.JSONDecodeError as exc:
                    if attempt == 1:
                        raise HTTPException(
                            status_code=502, detail=f"model did not return valid JSON: {exc}"
                        ) from exc
        except ModelUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

        try:
            return SkillDraftOut.model_validate(raw)
        except ValidationError as exc:
            raise HTTPException(
                status_code=502, detail=f"model draft did not match the expected shape: {exc}"
            ) from exc

    def _level_directory_context(state, level) -> Optional[str]:
        if not level or not level.directory_keys:
            return None
        by_key = {e["key"]: e["value"] for e in state.directory}
        return "\n\n".join(by_key[k] for k in level.directory_keys if by_key.get(k)) or None

    def _level_block_names(level) -> Optional[list[str]]:
        """Block names a level may use: those of its picked directory tabs.
        None (no restriction) when it picked none, or only custom keys that
        match no real tab."""
        if not level or not level.directory_keys:
            return None
        names = [b.name for b in block_directory_module.load() if b.tab in level.directory_keys]
        return names or None

    @app.post("/ask", response_model=AskResponse)
    async def ask(payload: AskRequest, request: Request) -> AskResponse:
        state = request.app.state

        async def handler():
            started = time.monotonic()
            session = state.sessions.load(payload.device_id)
            question_for_model = payload.question
            if payload.mode == "debug":
                history = _debug_conversation_history(session)
                if history is not None:
                    # _DEBUG_EXTRA's "ask AT MOST ONE question" rule is soft
                    # prompt guidance, not schema-enforced -- DebugAnswer.kind
                    # allows a second "ask" turn structurally, nothing stops
                    # it. Caught live: a real thread asked a clarifying
                    # question, the student answered it, and the very next
                    # turn asked ANOTHER question instead of committing to a
                    # diagnosis. A reminder tied to the EXACT fact that this
                    # specific thread already has one open question is a much
                    # stronger signal than the general system-prompt rule
                    # alone, which the model has to recall unaided turns
                    # later. Appended only to what the MODEL sees -- the
                    # student's own turn is still logged as their real
                    # words, see `student_turn` below.
                    question_for_model = (
                        payload.question
                        + '\n\n(You already asked your one allowed clarifying question in '
                        'this thread and the student just answered it. You MUST respond '
                        'with kind="answer" now -- a real diagnosis with a real `fault` '
                        'and corrected `blocks` -- not another question, even if some '
                        'detail is still unclear.)'
                    )
            else:
                history = _conversation_history(session, payload.mode)
                if payload.continue_thread and payload.mode == "build":
                    # Only overrides when there's actually a completed
                    # program to extend -- an open ask/phase thread already
                    # got its correct history above, and continue_thread
                    # being set on a stale/nonexistent session just leaves
                    # `history` as the `None` _conversation_history
                    # already returned.
                    history = _continuation_history_after_complete(session) or history

            # Economy mode: Build only, and only when a skill-doc actually
            # matches well enough to serve directly (app/llm/economy.py) —
            # a miss falls straight through to the normal full-AI path
            # below, same as if the toggle were off. No LLM call at all on
            # a hit, so this branch never touches state.client.
            if payload.mode == "build" and payload.economy:
                economy_answer = await try_economy_answer(
                    routing_text(payload.question, history), state.skills, state.embedder
                )
                if economy_answer is not None:
                    state.sessions.append_turn(
                        payload.device_id,
                        {"role": "student", "mode": "build", "question": payload.question, "image_note": None},
                        language=payload.language,
                    )
                    # Deliberately no "answer" key (unlike the full-AI turn
                    # below) — _conversation_history() treats that as a
                    # closed thread, so a follow-up starts fresh rather
                    # than trying to continue a non-existent ask/phase
                    # conversation. Matches the design: each economy
                    # request is an independent RAG lookup, not a
                    # multi-turn thread.
                    state.sessions.append_turn(
                        payload.device_id,
                        {"role": "assistant", "mode": "build", "economy": vars(economy_answer)},
                    )
                    state.log.append(
                        {
                            "device_id": payload.device_id,
                            "mode": payload.mode,
                            "language": payload.language,
                            "question": payload.question,
                            "had_image": False,
                            "economy": vars(economy_answer),
                            "latency_seconds": time.monotonic() - started,
                        }
                    )
                    return economy_answer

            level = next((lv for lv in state.levels if lv.id == payload.level), None)
            try:
                result = await run_pipeline(
                    client=state.client,
                    mode=payload.mode,
                    question=question_for_model,
                    language=payload.language,
                    image_base64=payload.image_base64,
                    history=history,
                    skills=state.skills,
                    embedder=state.embedder,
                    prompts=state.prompts,
                    level_prompt=level.prompt if level else None,
                    directory_context=_level_directory_context(state, level),
                    allowed_block_names=_level_block_names(level),
                )
            except ModelUnavailable as exc:
                state.log.append(
                    {
                        "device_id": payload.device_id,
                        "mode": payload.mode,
                        "language": payload.language,
                        "question": payload.question,
                        "had_image": payload.image_base64 is not None,
                        "error": str(exc),
                    }
                )
                raise

            # Belt and suspenders: a real completion means the model is
            # unambiguously loaded, regardless of whether startup's own
            # warmup() succeeded (e.g. Ollama was still starting when the
            # server came up).
            state.model_warm = True

            student_turn = {
                "role": "student",
                "mode": payload.mode,
                "question": payload.question,
                "image_note": result.answer.image_note if result.answer else None,
            }
            state.sessions.append_turn(payload.device_id, student_turn, language=payload.language)
            if result.answer:
                state.sessions.append_turn(
                    payload.device_id,
                    {"role": "assistant", "mode": payload.mode, "answer": result.answer.model_dump(mode="json")},
                )

            state.log.append(
                {
                    "device_id": payload.device_id,
                    "mode": payload.mode,
                    "language": payload.language,
                    "question": payload.question,
                    "had_image": payload.image_base64 is not None,
                    "answer": result.answer.model_dump(mode="json") if result.answer else None,
                    "validation_errors": [e.__dict__ for e in result.errors],
                    "model": result.model,
                    "used_fallback": result.used_fallback,
                    "fallback_reason": result.fallback_reason,
                    "latency_seconds": time.monotonic() - started,
                }
            )
            return result

        try:
            job = state.queue.submit(handler)
        except QueueFull:
            raise HTTPException(status_code=503, detail="busy, try again shortly")

        position = state.queue.position(job.id) or 1
        wait = state.queue.estimated_wait_seconds(job.id)
        return AskResponse(job_id=job.id, position=position, estimated_wait_seconds=list(wait) if wait else None)

    @app.get("/answer/{job_id}", response_model=AnswerResponse)
    async def get_answer(job_id: int, request: Request) -> AnswerResponse:
        state = request.app.state
        job = state.queue.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="unknown job")

        if job.status in (JobStatus.queued, JobStatus.running):
            position = state.queue.position(job_id)
            wait = state.queue.estimated_wait_seconds(job_id)
            return AnswerResponse(
                status=job.status.value,
                position=position,
                estimated_wait_seconds=list(wait) if wait else None,
            )

        if job.status == JobStatus.failed:
            return AnswerResponse(status="failed", error_message=job.error)

        if isinstance(job.result, EconomyAnswer):
            return AnswerResponse(status="done", economy=EconomyAnswerOut(**vars(job.result)))

        result: PipelineResult = job.result
        return AnswerResponse(
            status="done",
            answer=result.answer.model_dump(mode="json") if result.answer else None,
            render=render_tree(result.answer, result.errors) if result.answer else None,
            errors=[e.__dict__ for e in result.errors],
            fallback_notice=(
                "Switched to a backup AI model for this answer — the usual one was temporarily unavailable."
                if result.used_fallback
                else None
            ),
        )

    # Cached once, not re-read from disk per blocked request -- a static
    # file that never changes at runtime, unlike the actual app pages.
    _site_login_html = (config.REPO_ROOT / "app" / "static" / "site_login.html").read_text(encoding="utf-8")
    # Requests that must reach their real handler even with no valid site
    # cookie yet: /site-login (the only way to ever GET a valid cookie) and
    # /health (so external monitoring keeps working regardless of the
    # gate). Everything else -- including the admin panel and all its own
    # separate auth -- sits behind this gate too once the app is reachable
    # over the public internet.
    _SITE_GATE_EXEMPT_PATHS = {"/site-login", "/health"}

    @app.middleware("http")
    async def _site_gate(request: Request, call_next):
        if request.url.path in _SITE_GATE_EXEMPT_PATHS:
            return await call_next(request)
        if request.app.state.site_auth.check_cookie(request.cookies.get(COOKIE_NAME)):
            return await call_next(request)
        # Not authenticated -- serve the gate page directly rather than a
        # redirect, so the URL the student was actually trying to reach
        # isn't lost (a redirect to a fixed /login path would need its own
        # "return to where you came from" bookkeeping this avoids).
        return Response(content=_site_login_html, status_code=401, media_type="text/html")

    @app.post("/site-login")
    async def site_login(payload: SiteLoginRequest, request: Request) -> Response:
        cookie_value = request.app.state.site_auth.issue_cookie(payload.password)
        if cookie_value is None:
            raise HTTPException(status_code=401, detail="wrong password, or site login is not configured")
        response = Response(status_code=200)
        response.set_cookie(
            COOKIE_NAME,
            cookie_value,
            max_age=int(request.app.state.site_auth.ttl_seconds),
            httponly=True,
            samesite="lax",
        )
        return response

    # The real frontend (app/static/index.html) — mounted last so it never
    # shadows the /ask, /answer, /health routes above. NoCacheStaticFiles
    # (not the base StaticFiles) so an edited page is never served stale
    # from a device's own cache -- see its own docstring.
    app.mount("/", NoCacheStaticFiles(directory=config.REPO_ROOT / "app" / "static", html=True), name="static")

    return app


app = create_app()
