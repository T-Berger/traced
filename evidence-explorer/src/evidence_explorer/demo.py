"""Explicitly fictional examples and deterministic offline demonstration analysis.

Demo vectors are hashed word counts, not BGE or neural semantic embeddings.
Curated claims are fixtures, not predictions. Live Ollama uses separate cache keys.
"""

import hashlib
import json
import re
import unicodedata
from importlib.resources import files

import numpy as np

from .chunking import chunk_text
from .models import ArticleInput, ClaimInput
from .records import ArticleRecord, ChunkRecord

DEMO_EMBEDDING_KEY = "demo:hashed-words-v1"
DEMO_CLAIMS_KEY = "demo:curated-claims-v1"
DEMO_DIMENSIONS = 256


def sample_articles() -> list[dict]:
    resource = files("evidence_explorer").joinpath("data/sample_articles.json")
    return json.loads(resource.read_text(encoding="utf-8"))


def demo_embed(texts: list[str]) -> list[list[float]]:
    """A small reproducible lexical baseline for offline queue/search demonstrations."""
    stopwords = {
        "a",
        "an",
        "the",
        "is",
        "was",
        "were",
        "of",
        "and",
        "to",
        "in",
        "at",
        "on",
        "this",
        "for",
        "from",
        "after",
        "its",
        "with",
        "that",
        "said",
        "as",
        "be",
        "die",
        "der",
        "das",
        "den",
        "dem",
        "ein",
        "eine",
        "und",
        "im",
        "am",
        "von",
        "article",
        "fictional",
        "created",
        "evidence",
        "explorer",
        "demo",
        "fiktives",
    }
    vectors = []
    for text in texts:
        words = re.findall(r"\w+", unicodedata.normalize("NFKC", text).lower())
        vector = np.zeros(DEMO_DIMENSIONS, dtype=np.float32)
        for word in words:
            if word not in stopwords and len(word) > 1:
                position = int.from_bytes(hashlib.sha256(word.encode()).digest()[:4], "big")
                vector[position % DEMO_DIMENSIONS] += 1
        norm = float(np.linalg.norm(vector))
        if norm:
            vector /= norm
        vectors.append(vector.tolist())
    return vectors


def demo_claims(article: ArticleRecord, chunks: list[ChunkRecord]) -> list[ClaimInput]:
    sample = next((a for a in sample_articles() if a["url"] == article.url), None)
    if sample is None or sample["text"] != article.text:
        return []
    claims = []
    for fixture in sample["sample_claims"]:
        chunk = next((c for c in chunks if fixture["quote"] in c.text), None)
        if chunk:
            claims.append(ClaimInput(**fixture, chunk_id=chunk.id))
    return claims


def seed_demo(repo) -> int:
    """Seed once, preserving user edits and results on later application starts."""
    if repo.get_meta("demo_seed_version") == "1":
        return 0
    before = repo.articles.count()
    for fixture in sample_articles():
        article = ArticleInput(
            **{k: v for k, v in fixture.items() if k != "sample_claims"}, is_sample=True
        )
        article_id = repo.upsert_article(article)
        chunks = repo.passages.list_chunks(article_id)
        if not chunks:
            chunks = repo.passages.replace_chunks(article_id, chunk_text(article.text))
        for chunk, vector in zip(chunks, demo_embed([c.text for c in chunks]), strict=True):
            repo.embeddings.save_embedding(chunk.id, DEMO_EMBEDDING_KEY, vector)
        row = repo.articles.get_article(article_id)
        if row is None:
            raise ValueError("A demo article was removed while seeding.")
        claims = demo_claims(row, chunks)
        repo.replace_claims(
            article_id,
            claims,
            DEMO_CLAIMS_KEY,
            expected_content_hash=row.content_hash,
            cache_key=f"claims:{article_id}:{row.content_hash}:{DEMO_CLAIMS_KEY}",
        )
    repo.set_meta("demo_seed_version", "1")
    return repo.articles.count() - before
