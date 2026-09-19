"""Select which tabs of the Block Directory go into a request's context.

See docs/adr/0002-static-keyword-routing-instead-of-model-decided-retrieval.md.
Routing is a keyword lookup in our own code, not a model decision: under
Ollama's serialised inference (NUM_PARALLEL defaults to 1), a model-decided
retrieval step would occupy the queue twice per Enquiry. A keyword table
costs microseconds and no queue slot.

Over-fetching (sending a tab that turns out not to be needed) costs a few
hundred tokens. Under-fetching removes the block the student actually
needed, and the model papers over the gap with something that looks
plausible — the exact failure ADR-0001's enum exists to prevent elsewhere.
So ties and ambiguity both resolve toward sending more, not less, and
routing that finds nothing falls back to the whole directory: with only 64
blocks, "send everything" is a perfectly good outcome.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

from app.schema import block_directory as bd

# Strategy patterns like line-following (multiple sensors, junction
# detection, reusable turn functions) used to be a single hardcoded
# vendored doc injected here by keyword match. That's now a general,
# teacher-editable skill-doc library retrieved separately by
# app/skills/retrieve.py (embedding similarity, with its own keyword
# fallback) and injected in app/llm/pipeline.py — this module now routes
# the Block Directory only, per ADR-0002.

_STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "at", "is", "it",
    "its", "for", "with", "by", "as", "if", "then", "else", "this", "that",
    "featuring", "features", "feature", "field", "fields", "block", "blocks",
    "standard", "standalone", "embedded", "trailing", "initial", "including",
    "includes", "contains", "containing", "shape", "shaped", "menu", "text",
    # Catalogue meta-vocabulary: describes how a UI widget is drawn, not what
    # it does. Real signal without these; keeping them just causes spurious
    # ties across unrelated tabs.
    "defaulted", "configuration", "dropdown", "button", "area", "inline",
    "hardware", "appending", "explicitly", "generally", "needed", "avoid",
    "advanced", "not", "command", "input", "index", "activation",
    "adjustment", "auto", "numeric", "rounded", "elongated", "pointed",
}

# Hand-curated: a student's real vocabulary ("loop", "turn", "beep") often
# doesn't literally appear in the directory's own block text, so mechanical
# tokenisation alone under-routes common phrasing. This is a genuine editorial
# addition, not derived from the source — flagged as such rather than
# pretending it's automatic. Keep it short; the mechanical index below is the
# bulk of the coverage.
_SYNONYMS: dict[str, list[str]] = {
    "loop": ["Control"],
    "loops": ["Control"],
    "repeat": ["Control"],
    "wait": ["Control"],
    "stop": ["Control"],
    "start": ["Control"],
    "line": ["Sensing", "Motion"],
    "follow": ["Sensing", "Motion"],
    "sensor": ["Sensing"],
    "distance": ["Sensing"],
    "colour": ["Sensing"],
    "color": ["Sensing"],
    "turn": ["Motion"],
    "drive": ["Motion"],
    "wheel": ["Motion"],
    "wheels": ["Motion"],
    "speed": ["Motion"],
    "move": ["Motion"],
    "beep": ["Sound"],
    "music": ["Sound"],
    "note": ["Sound"],
    "led": ["Screen"],
    "display": ["Screen"],
    "print": ["Screen"],
    "count": ["Variable", "Calculation"],
    "variable": ["Variable"],
    "add": ["Variable", "Calculation"],
    "function": ["Function"],
    "procedure": ["Function"],
}


def _tokenize(text: str) -> set[str]:
    stripped = re.sub(r"\[[^\]]*\]", " ", text)
    words = re.findall(r"[a-zA-Z]+", stripped.lower())
    return {w for w in words if len(w) >= 3 and w not in _STOPWORDS}


@lru_cache(maxsize=1)
def _keyword_index() -> dict[str, set[str]]:
    """tab -> set of words drawn from its own blocks' names/text/remarks."""
    index: dict[str, set[str]] = {}
    for block in bd.load():
        words = _tokenize(block.name) | _tokenize(block.exact_text) | _tokenize(block.remarks)
        index.setdefault(block.tab, set()).update(words)
    return index


@dataclass
class RouteResult:
    tabs: list[str]
    matched_keywords: dict[str, list[str]]
    fallback: bool  # True when nothing matched and the whole directory is used


def route(question: str) -> RouteResult:
    question_words = _tokenize(question)
    index = _keyword_index()

    scores: dict[str, set[str]] = {}
    for tab, tab_words in index.items():
        hits = question_words & tab_words
        if hits:
            scores.setdefault(tab, set()).update(hits)
    for word in question_words:
        for tab in _SYNONYMS.get(word, []):
            scores.setdefault(tab, set()).add(word)

    if not scores:
        return RouteResult(tabs=bd.tabs(), matched_keywords={}, fallback=True)

    # Over-fetch on ties: every tab with at least one hit is included, not
    # just the top scorer.
    tabs = [t for t in bd.tabs() if t in scores]
    return RouteResult(
        tabs=tabs,
        matched_keywords={t: sorted(scores[t]) for t in tabs},
        fallback=False,
    )


def context_for(question: str) -> str:
    """The Block Directory text to inject into the model's context for this
    question: either the routed tabs' real markdown, or the whole file."""
    result = route(question)
    return bd.full_text() if result.fallback else "\n\n".join(
        bd.raw_tab_sections()[tab] for tab in result.tabs
    )
