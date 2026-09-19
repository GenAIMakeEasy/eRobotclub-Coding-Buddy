"""Picks at most one skill-doc to inject into a request's context, given the
student's question (+ conversation history) — the RAG half of the Ollo
skill library: a teacher writes/edits patterns via the admin panel
(app/api/main.py's /admin/skills routes, app/skills/store.py), and this
module finds the one that actually applies to what the student asked,
instead of asking the model to invent a strategy from nothing.

Cosine similarity over embeddings (app/rag/embeddings.py) is the primary
path; a plain keyword match against each skill-doc's own `triggers` is the
fallback — used both when no embedder was wired in at all, and when the
embedding backend is unreachable at request time. Degraded relevance, not a
missing feature, over one service hiccup.

Top-1 only, deliberately: this session already hit a real output-length
truncation bug (app/llm/ollama_client.py's num_predict) once one skill-doc's
worth of extra structure (nested `Logical And / Or` conditions) inflated
response size — injecting two or three candidate skill-docs at once would
make that worse for a marginal recall gain.
"""

from __future__ import annotations

import math
import re
from typing import Optional

from app.rag.embeddings import EmbeddingUnavailable, OllamaEmbedder
from app.skills.models import SkillDoc

# Below this, the best match is probably a coincidence (shared domain
# vocabulary like "robot" or "code") rather than a real match — inject
# nothing rather than force an irrelevant pattern onto an unrelated
# request. Measured live against nomic-embed-text with the seeded
# "line-following" skill-doc, across a wider set of phrasings than the
# first pass that picked 0.6:
#   relevant:   0.563-0.768 ("give code on line tracing" is the low end —
#               short, colloquial phrasing scores lower than a full
#               sentence like "follow the black line around corners")
#   unrelated:  0.334-0.538 ("make the robot beep three times" is the high
#               end — shares "robot"/domain vocabulary with every skill)
# 0.6 excluded real matches like "give code on line tracing" (0.563) as
# false negatives. 0.55 is the largest threshold that still excludes every
# unrelated example measured. The gap between the two clusters is only
# ~0.025 wide here — genuinely thin, not a comfortable margin — so this
# will misclassify some real phrasing either way; revisit once there are
# enough real skill-docs (this is calibrated against a single document) to
# measure a proper margin, or consider a hybrid keyword+embedding score
# instead of embedding alone.
_SIMILARITY_THRESHOLD = 0.55


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def _tokenize(text: str) -> set[str]:
    return set(re.findall(r"[a-zA-Z]+", text.lower()))


def _keyword_match(question_text: str, skills: list[SkillDoc]) -> Optional[SkillDoc]:
    words = _tokenize(question_text)
    best, best_hits = None, 0
    for skill in skills:
        hits = sum(1 for t in skill.triggers if t.lower() in words)
        if hits > best_hits:
            best, best_hits = skill, hits
    return best


def embed_text(doc: SkillDoc) -> str:
    """Canonical text a skill-doc's embedding is computed from — name,
    intro, trigger words, and every function's name and body. The bodies
    are pseudocode (sensor names, conditions, action words), which carries
    real topical signal even though it's HOW rather than WHEN a pattern
    applies — with no separate structured "when this applies" fields left
    (see models.py), this is what there is to embed."""
    parts = [doc.name, doc.intro, " ".join(doc.triggers)]
    parts += [f"{fn.name} {fn.body}" for fn in doc.functions]
    return " ".join(p for p in parts if p)


async def compute_embedding(doc: SkillDoc, embedder: OllamaEmbedder) -> list[float]:
    try:
        return await embedder.embed(embed_text(doc))
    except EmbeddingUnavailable:
        # A skill-doc saved while Ollama happens to be down still saves —
        # just without a usable embedding until the next edit recomputes
        # one. It still works via the keyword-match fallback in the
        # meantime, so this is degraded, not broken.
        return []


async def retrieve(
    question_text: str,
    skills: list[SkillDoc],
    embedder: Optional[OllamaEmbedder] = None,
) -> Optional[SkillDoc]:
    """`question_text` is the routing text the caller already builds
    (history + latest question — same convention as
    app/context/router.py's routing_text, so a continuation reply like "Yes,
    build it" still resolves against the original request's topic).

    `embedder` is opt-in, not defaulted to a real one here — matches the
    rest of this codebase's dependency style (ChatClient, QueueManager,
    ...), and keeps this function free of network calls unless a caller
    deliberately wires one in (app/api/main.py's real /ask path does;
    tests generally don't)."""
    if not skills:
        return None
    if embedder is None:
        return _keyword_match(question_text, skills)

    try:
        query_vec = await embedder.embed(question_text)
    except EmbeddingUnavailable:
        return _keyword_match(question_text, skills)

    scored = [(s, _cosine(query_vec, s.embedding)) for s in skills if s.embedding]
    if not scored:
        return _keyword_match(question_text, skills)

    best, score = max(scored, key=lambda pair: pair[1])
    if score < _SIMILARITY_THRESHOLD:
        return None
    return best
