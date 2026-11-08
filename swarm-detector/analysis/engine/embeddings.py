"""Stored sentence embeddings: where they live, how they were made, how to load them.

Vectors are created once by `embed.py` (needs the `embeddings` dependency group, i.e. PyTorch)
and read here with numpy only, so `run.py` never re-embeds.

    embeddings/<model>/<kind>.npy        float16, L2-normalised, one row per text
    embeddings/<model>/<kind>_ids.npy    comment (or post) id for each row
    embeddings/<model>/<kind>.json       model, recipe, row count, id fingerprint, timestamp
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

import numpy as np

from .config import ANALYSIS

EMBEDDINGS = ANALYSIS / "embeddings"

MODELS = {
    # 384 dims, 128-token window, trained on paraphrase pairs in 50+ languages. Fast.
    "minilm": {"hf": "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2", "prefix": "", "max_tokens": 128},
    # 768 dims, trained with "query: " / "passage: " prefixes; "query: " on both sides for symmetric
    # similarity. Window capped at 256 tokens: comments are short and it halves the run time.
    "e5": {"hf": "intfloat/multilingual-e5-base", "prefix": "query: ", "max_tokens": 256},
}

KINDS = {
    "content": "the comment text alone",
    "context": "the comment text followed by the start of the post it sits under",
    "posts": "the post texts (for post_relevance)",
}

CONTEXT_POST_CHARS = 400  # how much of the post goes into a context vector

EMBED_CMD = ("uvx uv@0.11.3 run --locked --package swarm-detector --group analysis --group embeddings "
             "python swarm-detector/analysis/embed.py --model {model} --kind {kind}")


class EmbeddingsError(RuntimeError):
    pass


@dataclass
class Vectors:
    model: str
    kind: str
    ids: np.ndarray
    X: np.ndarray  # float32, L2-normalised
    meta: dict

    def rows(self, ids) -> np.ndarray:
        """Row index for each id, -1 where the id has no vector."""
        pos = {v: i for i, v in enumerate(self.ids)}
        return np.array([pos.get(int(i), -1) for i in ids])


def embeddable(comments) -> np.ndarray:
    """Comments that get a vector: real text, not media-only or emoji/'+' only."""
    return (~comments.is_media_only & ~comments.is_low_info).to_numpy()


def fingerprint(ids) -> str:
    return hashlib.sha1(np.sort(np.asarray(ids, dtype=np.int64)).tobytes()).hexdigest()[:16]


def paths(model: str, kind: str):
    d = EMBEDDINGS / model
    return d / f"{kind}.npy", d / f"{kind}_ids.npy", d / f"{kind}.json"


def command(model: str, kind: str) -> str:
    return EMBED_CMD.format(model=model, kind=kind)


def load(model: str, kind: str, expected_ids=None) -> Vectors:
    if model not in MODELS:
        raise EmbeddingsError(f"Unknown embedding model '{model}'. Choose one of: {', '.join(MODELS)}.")
    if kind not in KINDS:
        raise EmbeddingsError(f"Unknown embedding kind '{kind}'. Choose one of: {', '.join(KINDS)}.")
    x_path, id_path, meta_path = paths(model, kind)
    if not (x_path.exists() and id_path.exists() and meta_path.exists()):
        raise EmbeddingsError(
            f"No '{model}' {kind} embeddings in {x_path.parent}.\n"
            f"They are created once (not on every run) and git-ignored. From the repo root run:\n\n"
            f"    {command(model, kind)}\n\n"
            f"or skip semantic metrics with: run.py --no-semantic")
    meta = json.loads(meta_path.read_text())
    ids = np.load(id_path)
    if expected_ids is not None and meta.get("fingerprint") != fingerprint(expected_ids):
        raise EmbeddingsError(
            f"The '{model}' {kind} embeddings were made from different comments than the current "
            f"preprocessing (preprocessing or data changed since {meta.get('created', '?')}). Rebuild them:\n\n"
            f"    {command(model, kind)} --force")
    return Vectors(model, kind, ids, np.load(x_path).astype(np.float32), meta)


def centred(X: np.ndarray) -> np.ndarray:
    """Remove the corpus mean direction and renormalise.

    Sentence embeddings share a large common component, so raw cosines between unrelated texts
    are already high (anisotropy). Centring spreads them around 0 and makes similarities comparable
    between models.
    """
    Y = X - X.mean(axis=0, keepdims=True)
    return Y / np.linalg.norm(Y, axis=1, keepdims=True).clip(min=1e-9)
