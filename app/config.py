"""Settings, in one place. Values are the defaults settled during design
(design/spec.md, grill-with-docs session) — env vars override for deployment
without code changes."""

from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parents[1]

# When the process started, and a short hash of every .py file under app/
# AS OF THAT MOMENT -- both computed once at import time, both exposed via
# /health. This isn't a git repo (no commit hash to check against), and a
# LaunchAgent auto-restarting a stale process on crash means "the server is
# running" is not the same fact as "the server is running today's edit" --
# these two together answer that directly: STARTED_AT proves a restart
# actually happened since an edit, and CODE_FINGERPRINT proves what's
# running matches what's on disk right now (a restart that raced an
# in-progress save, or one that targeted the wrong checkout, would still
# show a fresh STARTED_AT but a fingerprint that doesn't match a fresh
# recompute of the same function against the current files).
STARTED_AT = time.time()


def _compute_code_fingerprint(app_dir: Path = REPO_ROOT / "app") -> str:
    h = hashlib.sha256()
    for path in sorted(app_dir.rglob("*.py")):
        h.update(path.read_bytes())
    return h.hexdigest()[:12]


CODE_FINGERPRINT = _compute_code_fingerprint()


def _load_dotenv(path: Path = REPO_ROOT / ".env") -> None:
    """Minimal .env loader — no new dependency for one file. Existing
    environment variables always win, so a real deployment can still
    override without editing .env."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


def read_dotenv_file(path: Path) -> dict[str, str]:
    """Read a .env file's keys directly, without touching os.environ or
    falling back to process-wide config values. Used to seed admin_settings
    from the SAME file /admin/settings writes to — reading `config.*`
    instead would seed from whatever .env this process happened to start
    with, not the (possibly different, e.g. a test's tmp_path) file the
    admin endpoints actually read and write."""
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


def write_dotenv_updates(updates: dict[str, str], path: Path = REPO_ROOT / ".env") -> None:
    """Persist settings changes back to .env, so an admin's change (backend,
    model, API key) survives a server restart — not just the live hot-swap
    the /admin/settings endpoint also does. Line-based: rewrites matching
    keys in place, preserves every other line untouched, appends keys that
    weren't already present. Not a full .env parser (no quoting, no
    multiline values) — matches the same minimal scope as `_load_dotenv`.
    """
    existing_lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    remaining = dict(updates)
    new_lines = []
    for line in existing_lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.partition("=")[0].strip()
            if key in remaining:
                new_lines.append(f"{key}={remaining.pop(key)}")
                continue
        new_lines.append(line)
    for key, value in remaining.items():
        new_lines.append(f"{key}={value}")
    path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")

    # Keep this process's own view consistent with what was just written --
    # os.environ.update() alone is not enough: MODEL_BACKEND, OLLAMA_MODEL,
    # GEMINI_MODEL, GEMINI_API_KEY etc. were already read into module-level
    # names at import time (`GEMINI_API_KEY = os.environ.get(...)`), and
    # updating os.environ afterward does not retroactively change an
    # already-bound name. Without this, code that reads `config.X` (rather
    # than re-reading the environment) would see a stale value until the
    # process restarts, even though the .env file and os.environ are both
    # already correct. Only the string-valued settings an admin can actually
    # change are eligible — this is not a generic hot-reload of everything
    # in this module (numeric settings need their str->type coercion, which
    # a blind setattr would skip).
    _MUTABLE_STRING_SETTINGS = {
        "MODEL_BACKEND", "OLLAMA_MODEL", "GEMINI_MODEL", "GEMINI_API_KEY", "OLLAMA_HOST",
        "OPENROUTER_MODEL", "OPENROUTER_VISION_MODEL", "OPENROUTER_FALLBACK_MODEL", "OPENROUTER_API_KEY", "ELEVENLABS_API_KEY",
        "ELEVENLABS_VOICE_ID",
    }
    os.environ.update(updates)
    for key, value in updates.items():
        if key in _MUTABLE_STRING_SETTINGS:
            globals()[key] = value


_load_dotenv()

# "local" (Ollama, the default and the design's stated primary — see Q7:
# "local is primary, not sacred") or "gemini" (cloud, real cost, requires
# GEMINI_API_KEY and internet). See docs/adr/0004-swappable-model-backend.md.
# An admin can change this at runtime (POST /admin/settings); this module-
# level value is only the value at process start.
MODEL_BACKEND = os.environ.get("MODEL_BACKEND", "local")

# Required for /admin/login. Unset (the default) means admin login is
# disabled outright — no default password, since that would be a real
# security hole on a LAN-reachable app.
#
# This module-level value is only the value at process start -- like
# MODEL_BACKEND above, `os.environ.setdefault` in `_load_dotenv` means it's
# frozen forever once read, so hand-editing .env's ADMIN_PASSWORD line
# while the server is already running had no effect until a restart
# (caught live: a fresh .env edit kept getting "wrong password" against
# the OLD value still held in this process's memory). AdminAuth doesn't
# use this constant directly for that reason -- see
# `current_admin_password()` below, which it calls fresh on every login
# attempt instead.
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD")


def current_admin_password(path: Path = REPO_ROOT / ".env") -> Optional[str]:
    """The admin password AS OF RIGHT NOW, not as of process start --
    re-reads .env's ADMIN_PASSWORD line directly (same file `/admin/settings`
    itself writes to) every call, so a hand-edit takes effect on the very
    next login attempt with no restart required. Falls back to the
    process-start `ADMIN_PASSWORD` constant when .env doesn't set it at all
    (a real env var, no .env file -- the original deployment shape this
    setting was designed for)."""
    return read_dotenv_file(path).get("ADMIN_PASSWORD") or ADMIN_PASSWORD


# Gates the WHOLE student-facing app (app/site_auth.py), not just admin --
# added when the app moved from LAN-only to reachable over the public
# internet via a Cloudflare Tunnel, so a stranger who finds the URL can't
# burn through the owner's OpenRouter/Gemini credits. Unset means the gate
# is simply off, same "no default, no password means disabled" convention
# as ADMIN_PASSWORD. Same live-reload reasoning as current_admin_password
# above -- a hand-edited .env takes effect on the next request, no restart.
SITE_PASSWORD = os.environ.get("SITE_PASSWORD")


def current_site_password(path: Path = REPO_ROOT / ".env") -> Optional[str]:
    return read_dotenv_file(path).get("SITE_PASSWORD") or SITE_PASSWORD


GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
# "gemini-2.5-flash" (the original default) started 404ing for any
# newly-created Google account's API key ("no longer available to new
# users") despite still showing up in the models-listing endpoint — caught
# live when a fresh key still failed after the old one was suspected of
# being the problem. "gemini-flash-latest" is a moving alias, not a pinned
# version, which is deliberate here: pinning to today's dated model just
# reproduces the same dead-end once THAT one is retired. See
# app/llm/gemini_client.py's docstring for the thinkingBudget fallout of
# whatever model this alias currently resolves to.
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-flash-latest")
GEMINI_TIMEOUT_SECONDS = float(os.environ.get("GEMINI_TIMEOUT_SECONDS", "60"))

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY")
# A second cloud fallback alongside Gemini, added after Gemini's free tier
# kept rate-limiting in real classroom use (observed live: 5 requests/
# minute).
#
# "openrouter/free" (OpenRouter's own auto-router across available free
# models) was the FIRST choice, on the theory that staying on an alias
# avoids the exact dead-end gemini-2.5-flash hit when Google deprecated it
# under a fixed name. That theory did not survive contact with a second
# live request: the very next call after a clean test routed to
# openai/gpt-oss-20b:free — the model already disqualified in
# openrouter_client.py's docstring for burning its whole budget in an
# unparsed `reasoning` field and returning `content: null`. The
# auto-router picks per-request, not per-account, so "it worked in
# testing" said nothing about the next call. Pinned to the one model
# actually verified reliable instead; the alias tradeoff only pays off if
# the alias is trustworthy, and this one wasn't.
# OpenRouter's free tier: 20 requests/minute, 50/day (1000/day after a
# one-time $10 credit purchase, which never expires).
OPENROUTER_MODEL = os.environ.get("OPENROUTER_MODEL", "google/gemma-4-26b-a4b-it:free")
# 60s (the original default) was too tight: caught live across a 12-model
# comparison for app/skills/demo.py's render_demo_blocks (the admin
# skill-save-time block render), several otherwise-successful calls took
# well over a minute end to end -- a real completion still in progress, not
# a stalled connection. 180s gives a slow-but-genuine generation room to
# finish rather than being cut off right before it would have succeeded.
OPENROUTER_TIMEOUT_SECONDS = float(os.environ.get("OPENROUTER_TIMEOUT_SECONDS", "180"))

# Per-attempt cut-off for a STUDENT question (app/llm/pipeline.py's answer()):
# an attempt still running after this many seconds is abandoned and retried
# once, since a slow provider's second try is usually fast (observed live).
# Separate from the backend timeouts above, which stay the hard ceiling and
# still govern admin-side calls like the skill-save block render. 0 = off
# (the default -- a slow local Ollama can legitimately need minutes).
ASK_ATTEMPT_TIMEOUT_SECONDS = float(os.environ.get("ASK_ATTEMPT_TIMEOUT_SECONDS", "0"))

# Caught live: switching OPENROUTER_MODEL to qwen/qwen3-30b-a3b-instruct-2507
# (better text/JSON quality, chosen after OPENROUTER_MODEL's own default
# above was pinned) silently broke every image-attached call -- OpenRouter
# rejected it outright with `{"error": {"message": "No endpoints found that
# support image input", "code": 404}}`, for BOTH a student's Debug photo
# attachment and the admin skills-draft-from-image endpoint. A model good at
# structured text output is not necessarily multimodal, and OPENROUTER_MODEL
# was never re-checked for that after being changed. Rather than pin the
# whole app back to a vision-capable model just for the rare image turns,
# route only THOSE requests to a separately configured model --
# openrouter_client.py's chat() picks this one whenever image_base64 is set.
# Defaults to OPENROUTER_MODEL's own original default above, which this
# module's docstring already verified live handles the real production
# system prompt correctly -- a proven choice, not a new guess.
OPENROUTER_VISION_MODEL = os.environ.get("OPENROUTER_VISION_MODEL", "google/gemma-4-26b-a4b-it:free")

# A second model to retry with when OPENROUTER_MODEL itself fails (rate
# limited, provider outage, etc.) -- see openrouter_client.py's chat() for
# the actual fallback logic. Empty disables fallback entirely (falls
# straight back to today's behavior: one failure surfaces as an error to
# the student). Defaults to a different vendor than OPENROUTER_MODEL's own
# default, on the theory that a single provider's own outage shouldn't take
# down the fallback with it.
OPENROUTER_FALLBACK_MODEL = os.environ.get("OPENROUTER_FALLBACK_MODEL", "deepseek/deepseek-v4-flash")

ELEVENLABS_API_KEY = os.environ.get("ELEVENLABS_API_KEY")
# A real secret key starts with "sk_" -- the "key ID" ElevenLabs' own
# dashboard shows by default is NOT it (caught live: a pasted value turned
# out to be that ID, not the secret, which is only ever shown once at
# creation/rotation). Unset (the default) means the /tts endpoint is
# disabled outright, same "no feature" fallback as ADMIN_PASSWORD being
# unset disables login -- there is no default key.
#
# Independent of MODEL_BACKEND -- this is TTS for the assistant's own
# clarifying/plan-confirmation questions (kind="ask", no blocks), not part
# of the Build/Debug pipeline at all, so it has no "which backend" concept.
ELEVENLABS_VOICE_ID = os.environ.get("ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM")  # "Rachel", a premade voice
# eleven_turbo_v2_5 was ElevenLabs' own default here until they deprecated
# it outright ("outclassed by Flash") -- eleven_flash_v2_5 costs the exact
# same 0.5 credits/character and is the actively maintained replacement,
# with lower latency to boot. Verified live against ElevenLabs' own current
# docs before switching, not assumed from memory.
ELEVENLABS_TTS_MODEL = os.environ.get("ELEVENLABS_TTS_MODEL", "eleven_flash_v2_5")
ELEVENLABS_TIMEOUT_SECONDS = float(os.environ.get("ELEVENLABS_TIMEOUT_SECONDS", "30"))

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
# History: qwen3-vl:8b -> gemma3:12b-it-qat (fixed fabricated blocks on a
# line-follow request) -> gemma4:12b-it-qat (this one). Switched after gemma3
# was caught live producing bare, empty "If the IR sensor value" condition
# blocks alongside debug-print Output blocks instead of real nested `if
# then` structures (real screenshot evidence, not a synthetic test). Same
# live scenario replayed against gemma4:12b-it-qat produced correct nesting
# — real `if then` blocks with the condition properly seated in `condition`
# — with zero validation errors. Also smaller on disk (7.2GB vs 8.9GB) and
# comparable speed. See scripts/compare_models.py for the harness used.
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "gemma4:12b-it-qat")
# A small, purpose-built embedding model, deliberately separate from the
# chat model above — the skill-doc RAG retrieval step (app/skills/retrieve.py)
# runs it on every Build/Debug request, so it needs to be fast and cheap
# rather than repurpose the much larger chat model for a task it isn't
# tuned for. ~274MB on disk.
OLLAMA_EMBED_MODEL = os.environ.get("OLLAMA_EMBED_MODEL", "nomic-embed-text")
# A live smoke test on this Mac mini took 58-89s for a moderately complex
# Debug request on a 9B TEXT model (no vision/image overhead) — well past a
# naive 60s guess. Raised again from 180 to 300 after a live multi-phase
# Build conversation on gemma4:12b-it-qat measured a real 204s turn (deep
# nested JSON, ~1800 output tokens, prompt tokens nowhere near the num_ctx
# budget so it wasn't context truncation) — 180s would have hard-failed
# that request for a real student mid-build. These are real measurements,
# not round numbers; see design/direction-approved.md's carried-forward
# "not yet proved" list.
OLLAMA_TIMEOUT_SECONDS = float(os.environ.get("OLLAMA_TIMEOUT_SECONDS", "300"))

# Ollama defaults to a 4096-token context window if this is never set. That
# was a latent bug this whole session: never noticed until a real request
# (system prompt + routed directory + line-following strategy doc + 2 turns
# of history) measured ~3500 input tokens, leaving almost no room for the
# 2048-token response budget — the model silently ran out of context
# mid-generation and produced garbled, unrelated JSON. Raised again to
# 12288 alongside num_predict's bump to 4096 (see ollama_client.py) — the
# full IR3-6 line-tracing cascade (each steering branch now correctly nests
# a "Logical And / Or" plus its two IR-sensor conditions, per the
# `conditions`-list fix) produces enough JSON that 8192 total started
# truncating mid-string on a real live run. Note: OLLAMA_NUM_PARALLEL
# parallel slots each pay for their own context, so raising this multiplies
# memory with QUEUE_CONCURRENCY.
OLLAMA_CONTEXT_LENGTH = int(os.environ.get("OLLAMA_CONTEXT_LENGTH", "12288"))

# Original default (2) matched OLLAMA_NUM_PARALLEL on the Mac mini (16GB),
# back when the chat model itself ran locally and each concurrent slot paid
# for its own 16GB-constrained context. With MODEL_BACKEND=openrouter (see
# .env), chat generation happens on OpenRouter's servers, not this machine
# -- the Mac mini's RAM only has to hold nomic-embed-text (~274MB) per
# concurrent request now, not the 8-9GB chat model. Raised to 5 in .env:
# students queueing behind each other during a busy club session was adding
# real wait time on top of already-slow generations, and the old RAM
# ceiling that justified "2" no longer applies to OpenRouter-routed
# traffic. If MODEL_BACKEND is ever switched back to "local", drop this
# back down (and match OLLAMA_NUM_PARALLEL) before raising it again.
QUEUE_CONCURRENCY = int(os.environ.get("QUEUE_CONCURRENCY", "2"))
QUEUE_MAX_DEPTH = int(os.environ.get("QUEUE_MAX_DEPTH", "20"))
QUEUE_MIN_WAIT_SECONDS = float(os.environ.get("QUEUE_MIN_WAIT_SECONDS", "10"))
QUEUE_MAX_WAIT_SECONDS = float(os.environ.get("QUEUE_MAX_WAIT_SECONDS", "30"))

# Session: per-device, swept after 7 days (Q4/Q6). Log: permanent, raw,
# never swept (Q5/Q6) — a future local summarising pass needs raw text.
SESSION_DIR = Path(os.environ.get("SESSION_DIR", REPO_ROOT / "data" / "sessions"))
SESSION_TTL_SECONDS = float(os.environ.get("SESSION_TTL_SECONDS", str(7 * 24 * 3600)))
LOG_DIR = Path(os.environ.get("LOG_DIR", REPO_ROOT / "data" / "logs"))
