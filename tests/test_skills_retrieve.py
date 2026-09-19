"""retrieve() has two paths: cosine similarity over embeddings (primary,
opt-in via a real embedder) and keyword matching against `triggers`
(fallback -- used both when no embedder is given at all, and when the
embedder raises EmbeddingUnavailable). A fake embedder stands in for the
network so these are deterministic."""

import asyncio

from app.rag.embeddings import EmbeddingUnavailable
from app.skills.models import Function, SkillDoc
from app.skills.retrieve import compute_embedding, embed_text, retrieve


def run(coro):
    return asyncio.run(coro)


class FakeEmbedder:
    """Maps fixed strings to fixed vectors so cosine similarity is exact
    and predictable, rather than depending on a real model's output."""

    def __init__(self, vectors: dict[str, list[float]], fail: bool = False):
        self.vectors = vectors
        self.fail = fail

    async def embed(self, text: str) -> list[float]:
        if self.fail:
            raise EmbeddingUnavailable("simulated outage")
        return self.vectors.get(text, [0.0, 0.0])


def skill(id, triggers=None, embedding=None) -> SkillDoc:
    return SkillDoc(id=id, name=id, triggers=triggers or [], embedding=embedding or [])


def test_no_skills_returns_none_without_touching_the_embedder():
    class ExplodingEmbedder:
        async def embed(self, text):
            raise AssertionError("should never be called when skills is empty")

    assert run(retrieve("anything", [], ExplodingEmbedder())) is None


def test_no_embedder_falls_back_to_keyword_matching():
    skills = [skill("line-following", triggers=["line", "junction"])]
    match = run(retrieve("how do I follow the line", skills, embedder=None))
    assert match.id == "line-following"


def test_no_embedder_and_no_keyword_hit_returns_none():
    skills = [skill("line-following", triggers=["line", "junction"])]
    assert run(retrieve("make it beep three times", skills, embedder=None)) is None


def test_embedder_failure_falls_back_to_keyword_matching():
    skills = [skill("line-following", triggers=["line"])]
    embedder = FakeEmbedder({}, fail=True)
    match = run(retrieve("follow the line please", skills, embedder))
    assert match.id == "line-following"


def test_best_cosine_match_wins_above_threshold():
    skills = [
        skill("line-following", embedding=[1.0, 0.0]),
        skill("obstacle-avoidance", embedding=[0.0, 1.0]),
    ]
    embedder = FakeEmbedder({"q": [0.99, 0.01]})
    match = run(retrieve("q", skills, embedder))
    assert match.id == "line-following"


def test_a_low_similarity_match_is_rejected_not_forced():
    skills = [skill("line-following", embedding=[1.0, 0.0])]
    embedder = FakeEmbedder({"q": [0.0, 1.0]})  # orthogonal -> similarity 0
    assert run(retrieve("q", skills, embedder)) is None


def test_skills_with_no_embedding_yet_fall_back_to_keyword_matching():
    # A skill saved while the embedder was down (compute_embedding degrades
    # to []) must still be reachable via its triggers, not silently
    # unmatchable forever.
    skills = [skill("line-following", triggers=["line"], embedding=[])]
    embedder = FakeEmbedder({"q": [1.0, 0.0]})
    match = run(retrieve("q about the line", skills, embedder))
    assert match.id == "line-following"


def test_embed_text_covers_name_intro_triggers_and_function_bodies():
    doc = SkillDoc(
        id="x",
        name="Line Following",
        triggers=["line", "junction"],
        intro="Multi-sensor steering.",
        functions=[Function(name="line_trace", body="if No.5 < IR: turn left")],
    )
    text = embed_text(doc)
    for expected in ("Line Following", "Multi-sensor steering.", "line", "junction", "line_trace", "No.5 < IR"):
        assert expected in text


def test_compute_embedding_degrades_to_empty_list_on_failure():
    doc = SkillDoc(id="x", name="X")
    embedder = FakeEmbedder({}, fail=True)
    assert run(compute_embedding(doc, embedder)) == []


def test_compute_embedding_uses_the_embed_text():
    doc = SkillDoc(id="x", name="X", intro="hello")
    embedder = FakeEmbedder({embed_text(doc): [1.0, 2.0, 3.0]})
    assert run(compute_embedding(doc, embedder)) == [1.0, 2.0, 3.0]
