"""Create sentence embeddings once and store them in embeddings/ (git-ignored).

    uvx uv@0.11.3 run --locked --package swarm-detector --group analysis --group embeddings \
        python swarm-detector/analysis/embed.py --model minilm --kind content

--model  minilm | e5 | all
--kind   content | context | posts | all
--force  rebuild even if the vectors exist and match the current comments

Text recipes
  content  the comment alone. URLs become the word "link" (a URL is dozens of meaningless tokens).
  context  "<comment> || post: <first 400 chars of the root post>". The comment comes FIRST: when the
           model's token window is full (MiniLM: 128 tokens) the end is cut, so the post is trimmed and
           the comment never is. Orphan comments (no root post) get their content text.
  posts    the post text, for post_relevance.
e5 gets its required "query: " prefix. Vectors are L2-normalised and saved as float16.
"""

from __future__ import annotations

import argparse
import json
import pickle
import re
import sys
import time
from datetime import UTC, datetime

from engine import readiness


def require_account_analysis() -> None:
    try:
        readiness.require_source_identity()
    except readiness.IdentityUnavailable as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    # Do not load legacy identity caches or download embedding models.
    require_account_analysis()

import numpy as np
import pandas as pd
from engine import config as cfg
from engine import embeddings as emb
from engine import preprocess

URL = preprocess.URL
SPACES = re.compile(r"\s+")


def clean(text: str) -> str:
    return SPACES.sub(" ", URL.sub(" link ", str(text))).strip()


def load_comments() -> pd.DataFrame:
    require_account_analysis()
    path = cfg.CACHE / "preprocessed.pkl"
    if path.exists():
        return pickle.loads(path.read_bytes()).comments
    print("No preprocessing cache yet: running preprocessing first.")
    pp = preprocess.run()
    cfg.CACHE.mkdir(exist_ok=True)
    path.write_bytes(pickle.dumps(pp))
    return pp.comments


def texts_for(kind: str, comments: pd.DataFrame) -> tuple[np.ndarray, list[str]]:
    if kind == "posts":
        items = pd.read_csv(cfg.DATA / "items.csv.gz", usecols=["id", "type", "description_orig"])
        posts = items[(items.type == preprocess.POST) & items.id.isin(comments.root_id.dropna().astype(int))]
        posts = posts[~posts.description_orig.fillna("No description").isin(["", "No description"])]
        return posts.id.to_numpy(), [clean(t) for t in posts.description_orig]
    c = comments[emb.embeddable(comments)]
    content = [clean(t) for t in c.text]
    if kind == "content":
        return c.id.to_numpy(), content
    items = pd.read_csv(cfg.DATA / "items.csv.gz", usecols=["id", "description_orig"])
    post_text = items.set_index("id").description_orig.reindex(c.root_id.astype("Int64").fillna(-1).astype(int))
    out = []
    for txt, post in zip(content, post_text.to_numpy(), strict=True):
        post = "" if pd.isna(post) or post == "No description" else clean(post)[:emb.CONTEXT_POST_CHARS]
        out.append(f"{txt} || post: {post}" if post else txt)
    return c.id.to_numpy(), out


def embed(model: str, kind: str, comments: pd.DataFrame, force: bool) -> None:
    x_path, id_path, meta_path = emb.paths(model, kind)
    ids, texts = texts_for(kind, comments)
    fp = emb.fingerprint(ids)
    if meta_path.exists() and not force and json.loads(meta_path.read_text()).get("fingerprint") == fp:
        print(f"{model}/{kind}: up to date ({len(ids):,} rows), skipping. Use --force to rebuild.")
        return

    import torch
    from sentence_transformers import SentenceTransformer

    spec = emb.MODELS[model]
    device = "mps" if torch.backends.mps.is_available() else "cuda" if torch.cuda.is_available() else "cpu"
    st = SentenceTransformer(spec["hf"], device=device)
    st.max_seq_length = spec["max_tokens"]
    print(f"{model}/{kind}: {len(texts):,} texts on {device} ({spec['hf']})")
    t0 = time.time()
    X = st.encode([spec["prefix"] + t for t in texts], batch_size=128, normalize_embeddings=True,
                  show_progress_bar=True, convert_to_numpy=True)
    x_path.parent.mkdir(parents=True, exist_ok=True)
    np.save(x_path, X.astype(np.float16))
    np.save(id_path, ids.astype(np.int64))
    tokens = st.tokenizer([spec["prefix"] + t for t in texts[:5000]], add_special_tokens=True)["input_ids"]
    meta_path.write_text(json.dumps({
        "model": model, "hf_name": spec["hf"], "kind": kind, "recipe": emb.KINDS[kind],
        "prefix": spec["prefix"], "max_tokens": spec["max_tokens"], "rows": len(ids), "dims": int(X.shape[1]),
        "fingerprint": fp, "device": device, "seconds": round(time.time() - t0, 1),
        "truncated_share_sample": float(np.mean([len(t) > spec["max_tokens"] for t in tokens])),
        "created": datetime.now(UTC).isoformat(timespec="seconds"),
    }, indent=2))
    print(f"{model}/{kind}: saved {X.shape} in {time.time() - t0:.0f} s -> {x_path}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", default="minilm", choices=[*emb.MODELS, "all"])
    ap.add_argument("--kind", default="all", choices=[*emb.KINDS, "all"])
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    if not (cfg.DATA / "items.csv.gz").exists():
        sys.exit(f"Missing {cfg.DATA / 'items.csv.gz'}: put the challenge export in swarm-detector/data/ first.")
    comments = load_comments()
    for model in emb.MODELS if a.model == "all" else [a.model]:
        for kind in emb.KINDS if a.kind == "all" else [a.kind]:
            embed(model, kind, comments, a.force)


if __name__ == "__main__":
    main()
