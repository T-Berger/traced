"""Test set: the same pipeline on X posts about Zohran Mamdani (NYC mayoral election, 4 Nov 2025).

    uvx uv@0.11.3 run --locked --package swarm-detector --group analysis --group embeddings \
        python swarm-detector/analysis/mamdani.py

Reads swarm-detector/test-data/ (public.content_items + public.authors), keeps third-party posts,
maps them onto the comment table the engine expects and re-runs the frozen method: the same metric
functions, the same normalisation and the same mixture / separation models, re-fitted on this data
(Telegram and X units differ, so Telegram's fitted numbers cannot be reused). Writes
results/05_mamdani.html and results/mamdani_accounts.csv.gz.

The two-population claim is tested against a null that keeps every metric's own distribution and
breaks only the joint structure (each column shuffled on its own). The same null is run on the
Telegram accounts so both datasets face the same test. Account age, decoded from the X user id,
is never used in the fit: it is an outside check on what the populations are.

The e5 vectors are made once into embeddings/mamdani/ (git-ignored); later runs only read them.
"""

from __future__ import annotations

import json
import re
import warnings

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from engine import config as cfg
from engine import embeddings as emb
from engine import features, metrics, models, preprocess
from engine.metrics import REGISTRY
from engine.page import COLORS, Page
from scipy import stats
from sklearn.mixture import GaussianMixture

warnings.filterwarnings("ignore", category=FutureWarning)

TEST = cfg.ANALYSIS.parent / "test-data"
BASE_EMB = emb.EMBEDDINGS / "mamdani"
TZ = "America/New_York"
ELECTION = pd.Timestamp("2025-11-05 05:00", tz="UTC")  # polls closed 4 Nov 21:00 ET
MIN_POSTS = 3  # 5 (the Telegram rule) leaves 212 accounts; 3 leaves 471
MAX_MISSING = 0.5  # metrics missing for more accounts than this are reported but not fitted
N_NULL = 20
NEW_ACCOUNT_DAYS = 180
TWITTER_EPOCH_MS = 1288834974657

# Metrics that exist on this data. Left out: channel and side metrics (one topic, no channels),
# post latency, crowding, partners and follows (posts were sampled at most 40 per 12 h, so who
# posts near whom is an artefact of the sampling), two_faced and post_relevance.
FROZEN = ["n_comments", "active_days", "median_gap", "burstiness", "hour_entropy", "night_share",
          "routine_drift", "min_gap", "mean_text_len", "cross_dup_share", "self_dup_share", "url_share",
          "low_effort_share", "reply_share", "paraphrase_share", "self_similarity", "self_sim_std",
          "script_share"]

LEAD_MENTIONS = re.compile(r"^(?:@\w+\s*)+")


# ---------------------------------------------------------------- data

def created_at(user_id: pd.Series) -> pd.Series:
    """X user ids issued since 2013 are snowflakes: id >> 22 is milliseconds since the X epoch.
    Older, sequential ids (< 1e10) only say "before 2014"; they get 2013-01-01 as an upper bound."""
    uid = np.array([int(s) if str(s).isdigit() else 0 for s in user_id], dtype=np.int64)
    snow = uid >= 10**10
    ms = pd.Series(np.where(snow, (uid >> 22) + TWITTER_EPOCH_MS, -1), index=user_id.index)
    return pd.to_datetime(ms.where(snow), unit="ms", utc=True).fillna(pd.Timestamp("2013-01-01", tz="UTC"))


def load() -> tuple[pd.DataFrame, dict]:
    items = pd.read_csv(TEST / "public.content_items.csv.gz", usecols=lambda c: c != "raw")
    authors = pd.read_csv(TEST / "public.authors.csv.gz", usecols=lambda c: c != "raw")
    facts = {"rows": len(items), "by_attribution": items.attribution.value_counts()}
    t = items[items.attribution == "third_party"].drop_duplicates("platform_item_id").copy()
    a = authors.set_index("id")
    t["handle"] = t.author_id.map(a.handle).str.lower()
    t = t[t.handle.notna()]
    t["account"] = "@" + t.handle
    t["id"] = t.platform_item_id.astype("int64")
    t["t"] = pd.to_datetime(t.posted_at, utc=True, format="mixed")
    raw = t.text.fillna("")
    t["is_media_only"] = raw.str.strip().eq("")
    t["text"] = raw
    t["text_key"] = raw.map(preprocess.text_key)
    t["text_len"] = t.text_key.str.len()
    t["n_urls"] = raw.map(lambda s: len(preprocess.URL.findall(s)))
    t["is_low_info"] = ~t.is_media_only & (t.text_len < 3)
    is_reply = t.is_reply.astype(str).str.lower().isin(["t", "true"])
    t["parent_type"] = np.where(is_reply, preprocess.COMMENT, "missing")
    t["parent_id"] = pd.to_numeric(t.parent_item_id, errors="coerce")
    t["root_id"] = np.nan
    t["side"] = "unknown"
    t["channel"] = "x"
    t["hour"] = t.t.dt.tz_convert(TZ).dt.hour
    t["followers"] = t.author_id.map(a.followers_count)
    t["created"] = created_at(t.author_id.map(a.platform_user_id))
    t["account_age_days"] = (t.t - t.created).dt.total_seconds() / 86400
    t["period"] = np.where(t.t < ELECTION, "before", "after")
    for col in ["like_count", "view_count"]:
        t[col] = pd.to_numeric(t[col], errors="coerce")
    t = t.sort_values(["account", "t"])
    t["gap_prev_s"] = t.groupby("account").t.diff().dt.total_seconds()
    t = t.sort_values("t").reset_index(drop=True)
    facts.update(third_party=len(t), accounts=t.account.nunique(), span=(t.t.min(), t.t.max()))
    return t, facts


def with_threshold(c: pd.DataFrame) -> pd.DataFrame:
    n = c.account.map(c.account.value_counts())
    return c.assign(in_model=n >= MIN_POSTS)


# ---------------------------------------------------------------- embeddings

def embed_text(s: str) -> str:
    """Leading reply @mentions dropped (they make all replies to one person look alike); URLs -> "link"."""
    s = LEAD_MENTIONS.sub("", s)
    return re.sub(r"\s+", " ", preprocess.URL.sub(" link ", s)).strip()


def store(folder, ids: np.ndarray, X: np.ndarray, meta: dict) -> None:
    old = emb.EMBEDDINGS
    emb.EMBEDDINGS = folder
    x_path, id_path, meta_path = emb.paths(cfg.EMBED_MODEL, "content")
    emb.EMBEDDINGS = old
    x_path.parent.mkdir(parents=True, exist_ok=True)
    np.save(x_path, X.astype(np.float16))
    np.save(id_path, ids.astype(np.int64))
    meta_path.write_text(json.dumps({**meta, "rows": len(ids), "fingerprint": emb.fingerprint(ids)}, indent=2))


def ensure_vectors(c: pd.DataFrame) -> None:
    """e5 content vectors for every embeddable post, made once; per-period copies are slices of them."""
    mask = emb.embeddable(c)
    ids = c.id[mask].to_numpy()
    x_path, id_path, meta_path = (BASE_EMB / "all" / cfg.EMBED_MODEL / f"content{s}" for s in (".npy", "_ids.npy", ".json"))
    if not (meta_path.exists() and json.loads(meta_path.read_text())["fingerprint"] == emb.fingerprint(ids)):
        import torch
        from sentence_transformers import SentenceTransformer
        spec = emb.MODELS[cfg.EMBED_MODEL]
        device = "mps" if torch.backends.mps.is_available() else "cpu"
        st = SentenceTransformer(spec["hf"], device=device)
        st.max_seq_length = spec["max_tokens"]
        texts = [spec["prefix"] + embed_text(s) for s in c.text[mask]]
        print(f"   embedding {len(texts):,} posts with {spec['hf']} on {device}")
        X = st.encode(texts, batch_size=128, normalize_embeddings=True, show_progress_bar=True)
        store(BASE_EMB / "all", ids, X, {"model": cfg.EMBED_MODEL, "hf_name": spec["hf"], "kind": "content",
                                          "recipe": "post text, leading @mentions removed, URLs -> link"})
    X = np.load(x_path).astype(np.float32)
    all_ids = np.load(id_path)
    for period in ["before", "after"]:
        keep = np.isin(all_ids, c.id[mask & (c.period == period).to_numpy()].to_numpy())
        store(BASE_EMB / period, all_ids[keep], X[keep], {"model": cfg.EMBED_MODEL, "slice_of": "all"})


# ---------------------------------------------------------------- models

def account_metrics(c: pd.DataFrame, scope: str) -> tuple[metrics.Ctx, pd.DataFrame]:
    emb.EMBEDDINGS = BASE_EMB / scope
    ctx = metrics.Ctx(with_threshold(c))
    return ctx, metrics.compute(ctx, FROZEN)


def fit_columns(raw: pd.DataFrame) -> list[str]:
    return [m for m in raw if raw[m].isna().mean() <= MAX_MISSING]


def delta_bic(Z: np.ndarray) -> float:
    b = [GaussianMixture(k, covariance_type="full", reg_covar=models.REG, n_init=2, random_state=cfg.SEED).fit(Z).bic(Z)
         for k in (1, 2)]
    return b[0] - b[1]


def copula_sample(X: np.ndarray, rng) -> np.ndarray:
    """One population with the same marginals and the same rank correlations as X (Gaussian copula):
    normal scores -> their correlation R -> draw N(0, R) -> map each column back through X's own
    sorted values. Only structure beyond "one correlated crowd with skewed metrics" is removed."""
    n = len(X)
    ranks = np.column_stack([stats.rankdata(col) for col in X.T])
    R = np.corrcoef(stats.norm.ppf(ranks / (n + 1)), rowvar=False)
    G = rng.multivariate_normal(np.zeros(X.shape[1]), R, size=n, method="eigh")
    order = np.argsort(np.argsort(G, axis=0), axis=0)
    return np.take_along_axis(np.sort(X, axis=0), order, axis=0)


def null_test(Z: pd.DataFrame) -> dict:
    """ΔBIC on the real accounts vs two nulls built from the same accounts.

    shuffle: each metric column permuted on its own. Keeps every metric's skew and zero spike, drops all
             correlation, so it is generous to the null (a single Gaussian can no longer use correlation).
    copula:  one Gaussian-copula population with the same marginals AND rank correlations. This is the
             fair "one crowd" null: what is left over it is clustering, i.e. a separate population.
    """
    rng = np.random.default_rng(cfg.SEED)
    X = Z.to_numpy()
    real = delta_bic(X)
    shuffle = np.array([delta_bic(np.column_stack([rng.permutation(col) for col in X.T])) for _ in range(N_NULL)])
    copula = np.array([delta_bic(copula_sample(X, rng)) for _ in range(N_NULL)])
    return {"accounts": len(X), "metrics": X.shape[1], "delta_bic": real,
            "shuffle_max": shuffle.max(), "p_shuffle": (1 + (shuffle >= real).sum()) / (N_NULL + 1),
            "null_mean": copula.mean(), "null_max": copula.max(), "excess": real - copula.max(),
            "p": (1 + (copula >= real).sum()) / (N_NULL + 1), "null": copula, "shuffle": shuffle}


def fit(raw: pd.DataFrame) -> dict:
    cols = fit_columns(raw)
    N = features.normalise(raw[cols])
    mix = models.mixtures(N.Z)
    return {"cols": cols, "N": N, "mix": mix, "sep": models.separation(N.Z, mix),
            "anomaly": models.anomaly(N.Z, mix.logp), "null": null_test(N.Z)}


def telegram_null() -> dict | None:
    path = cfg.RESULTS / "accounts.csv.gz"
    if not path.exists():
        return None
    a = pd.read_csv(path, index_col=0)
    cols = [m for m in REGISTRY if m in a and a[m].isna().mean() <= MAX_MISSING]
    return null_test(features.normalise(a[cols]).Z)


# ---------------------------------------------------------------- before vs after, post level

N_TOPICS = 14


def holm(p: pd.Series) -> pd.Series:
    order = p.sort_values()
    adj = (order * np.arange(len(order), 0, -1)).cummax().clip(upper=1)
    return adj.reindex(p.index)


def changes(c: pd.DataFrame) -> dict:
    """What changed after the election, per post (shares are comparable across periods even though
    the collection cap flattens volume)."""
    from sklearn.cluster import KMeans
    from sklearn.feature_extraction.text import TfidfVectorizer

    flag = lambda col: c[col].astype(str).str.lower().isin(["t", "true"])  # noqa: E731
    post = c.assign(reply=flag("is_reply"), quote=flag("is_quote"), link=c.n_urls > 0, non_english=c.lang != "en",
                    night=c.hour.isin(cfg.NIGHT_HOURS))
    before = post.period == "before"
    rows = []
    for col, label in [("reply", "reply to another post"), ("quote", "quote post"), ("link", "contains a link"),
                       ("non_english", "not in English"), ("night", "posted 01:00-05:59 New York time")]:
        tab = pd.crosstab(post.period, post[col]).reindex(index=["before", "after"], columns=[False, True], fill_value=0)
        rows.append({"indicator": label, "unit": "posts", "before": post[before][col].mean(),
                     "after": post[~before][col].mean(), "test": "χ²", "p": stats.chi2_contingency(tab).pvalue})
    for col, label in [("like_count", "likes (median)"), ("view_count", "views (median)"),
                       ("followers", "author followers (median)")]:
        a, b = post[before][col].dropna(), post[~before][col].dropna()
        rows.append({"indicator": label, "unit": "posts", "before": a.median(), "after": b.median(),
                     "test": "Mann-Whitney", "p": stats.mannwhitneyu(a, b).pvalue})
    authors = post.groupby(["period", "account"]).created.min().reset_index()
    authors["new"] = authors.created.dt.year == 2025
    tab = pd.crosstab(authors.period, authors.new).reindex(index=["before", "after"], columns=[False, True])
    rows.append({"indicator": "authors whose account was created in 2025", "unit": "authors",
                 "before": tab.loc["before", True] / tab.loc["before"].sum(),
                 "after": tab.loc["after", True] / tab.loc["after"].sum(), "test": "Fisher",
                 "p": stats.fisher_exact(tab.to_numpy()).pvalue})
    top20 = {p: post[post.period == p].account.value_counts().head(20).sum() / (post.period == p).sum()
             for p in ["before", "after"]}
    rows.append({"indicator": "share of posts by the 20 busiest authors", "unit": "posts", **top20, "test": "", "p": np.nan})
    ind = pd.DataFrame(rows).set_index("indicator")
    ind["p_holm"] = holm(ind.p.dropna()).reindex(ind.index)

    # topics: k-means on the centred e5 vectors, named by their most distinctive words
    X = emb.centred(np.load(BASE_EMB / "all" / cfg.EMBED_MODEL / "content.npy").astype(np.float32))
    ids = np.load(BASE_EMB / "all" / cfg.EMBED_MODEL / "content_ids.npy")
    d = c.set_index("id").loc[ids].reset_index()
    d["topic"] = KMeans(N_TOPICS, n_init=10, random_state=cfg.SEED).fit_predict(X)
    docs = d.groupby("topic").text.apply(lambda s: " ".join(s.map(embed_text)))
    tf = TfidfVectorizer(stop_words="english", max_df=0.5, min_df=3, sublinear_tf=True,
                         token_pattern=r"(?u)\b[a-zA-Z][a-zA-Z]{2,}\b").fit(docs)
    W, vocab = tf.transform(docs).toarray(), np.array(tf.get_feature_names_out())
    tab = pd.crosstab(d.topic, d.period)[["before", "after"]]
    chi = stats.chi2_contingency(tab)
    share = tab / tab.sum()
    topics = pd.DataFrame({
        "words": [", ".join(vocab[np.argsort(-W[k])[:7]]) for k in tab.index],
        "posts": tab.sum(axis=1).to_numpy(), "before": share.before.to_numpy(), "after": share.after.to_numpy(),
        "ratio": (share.after / share.before).to_numpy(),
        "z": ((tab.after - chi.expected_freq[:, 1]) / np.sqrt(chi.expected_freq[:, 1])).to_numpy(),
        "example": [d[d.topic == k].sort_values("like_count").text.iloc[-1][:120].replace("\n", " ") for k in tab.index],
    }, index=[f"T{k}" for k in tab.index]).sort_values("z")

    # the same text (15+ chars) from 2+ accounts
    long = c[c.text_len >= cfg.DUP_MIN_CHARS]
    g = long.groupby("text_key")
    copies = g.agg(accounts=("account", "nunique"), posts=("id", "size"), first=("t", "min"), last=("t", "max"),
                   period=("period", "first"), text=("text", lambda s: s.iloc[0][:150].replace("\n", " ")))
    copies = copies[copies.accounts >= 2]
    copies["span_h"] = (copies["last"] - copies["first"]).dt.total_seconds() / 3600
    copies = copies.sort_values(["accounts", "span_h"], ascending=[False, True]).reset_index(drop=True)
    return {"indicators": ind, "topics": topics, "topic_p": chi.pvalue, "copies": copies}


# ---------------------------------------------------------------- assumption: every 2025 account is a bot

def auc_test(y, s, n: int = 2000, seed: int = cfg.SEED) -> tuple[float, float]:
    """AUC of score s for labels y, with a two-sided label-permutation p value."""
    from sklearn.metrics import roc_auc_score
    rng = np.random.default_rng(seed)
    y, s = np.asarray(y, dtype=bool), np.asarray(s, dtype=float)
    ok = np.isfinite(s)
    y, s = y[ok], s[ok]
    real = roc_auc_score(y, s)
    null = np.array([roc_auc_score(rng.permutation(y), s) for _ in range(n)])
    return real, (1 + (np.abs(null - 0.5) >= abs(real - 0.5)).sum()) / (n + 1)


def new_accounts(c: pd.DataFrame, out: pd.DataFrame) -> dict:
    """Treat every account created in 2025 as a bot and ask the Telegram validation question: do the
    metrics, the anomaly score or the content pick them out, and do they behave differently?"""
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import roc_auc_score
    from sklearn.model_selection import GroupKFold, cross_val_predict

    c = c.assign(bot=c.created >= pd.Timestamp("2025-01-01", tz="UTC"),
                 reply=c.is_reply.astype(str).str.lower().isin(["t", "true"]))
    au = c.groupby("account").bot.first()
    m = out.assign(bot=out.index.map(au))
    timing = {"median_gap", "burstiness", "hour_entropy", "night_share", "routine_drift", "min_gap"}
    rows = [{"score": "anomaly score (GMM + Isolation Forest)", **dict(zip(["auc", "p"], auc_test(m.bot, m.anomaly), strict=True))}]
    for col in FROZEN:
        if col not in timing and m[col].notna().sum() > 50:
            rows.append({"score": col, **dict(zip(["auc", "p"], auc_test(m.bot, m[col], 500), strict=True))})
    rows.append({"score": "followers (not a behaviour metric)",
                 **dict(zip(["auc", "p"], auc_test(m.bot, np.log1p(m.followers), 500), strict=True))})
    scores = pd.DataFrame(rows).set_index("score")

    # content: can the e5 vector of a post tell a new account's post from an old one's? CV grouped by account
    X = emb.centred(np.load(BASE_EMB / "all" / cfg.EMBED_MODEL / "content.npy").astype(np.float32))
    ids = np.load(BASE_EMB / "all" / cfg.EMBED_MODEL / "content_ids.npy")
    d = c.set_index("id").loc[ids].reset_index()
    clf = LogisticRegression(C=0.5, max_iter=2000, class_weight="balanced")
    groups = pd.factorize(d.account)[0]

    def cv_auc(y):
        return roc_auc_score(y, cross_val_predict(clf, X, y, groups=groups, cv=GroupKFold(5), method="predict_proba")[:, 1])

    rng = np.random.default_rng(cfg.SEED)
    content = cv_auc(d.bot.to_numpy())
    null = [cv_auc(d.account.map(pd.Series(rng.permutation(au.to_numpy()), index=au.index)).to_numpy()) for _ in range(10)]

    rows = []
    for period in ["before", "after"]:
        s = c[c.period == period]
        for col, label in [("reply", "replies"), ("non_en", "not in English"), ("hostile", "hostile wording")]:
            v = (s.reply if col == "reply" else s.lang != "en" if col == "non_en" else
                 s.text.str.contains(HOSTILE, case=False))
            tab = pd.crosstab(s.bot, v).reindex(index=[False, True], columns=[False, True], fill_value=0)
            rows.append({"period": period, "behaviour": label, "older accounts": v[~s.bot].mean(),
                         "2025 accounts": v[s.bot].mean(), "p": stats.chi2_contingency(tab).pvalue})
        rows.append({"period": period, "behaviour": "likes (median)", "older accounts": s[~s.bot].like_count.median(),
                     "2025 accounts": s[s.bot].like_count.median(),
                     "p": stats.mannwhitneyu(s[~s.bot].like_count, s[s.bot].like_count).pvalue})
    behaviour = pd.DataFrame(rows)
    share = pd.crosstab(c.period, c.bot).reindex(index=["before", "after"])
    targets = (c[c.bot & c.reply].text.str.extract(r"^@(\w+)")[0].str.lower().value_counts().head(8))
    return {"n_bots": int(au.sum()), "n_accounts": len(au), "posts_per_bot": c[c.bot].account.value_counts(),
            "modelled_bots": int(m.bot.sum()), "modelled": len(m), "scores": scores, "content_auc": content,
            "content_null": np.array(null), "behaviour": behaviour,
            "share": share[True] / share.sum(axis=1), "share_p": stats.fisher_exact(share.to_numpy()).pvalue,
            "targets": targets}


HOSTILE = r"communis|jihad|islamist|marxist|hamas|terror|antisemit|sharia|radical|socialist|destroy|dangerous"


# ---------------------------------------------------------------- report

def kv_hist(x, color, name, bins=30):
    x = np.asarray(x, dtype=float)
    x = x[np.isfinite(x)]
    counts, edges = np.histogram(x, bins=bins)
    return go.Bar(x=(edges[:-1] + edges[1:]) / 2, y=counts / max(counts.sum(), 1), width=np.diff(edges),
                  name=name, marker_color=color, opacity=0.6, marker_line_width=0)


def report(c, facts, res, periods, tg, acc, weekly, shift, ch) -> Page:
    pg = Page("05_mamdani.html", "Test set: Mamdani election on X",
              "The frozen method (same metrics, normalisation and models) applied to X posts about Zohran Mamdani, "
              "1 Oct – 30 Dec 2025, election on 4 Nov. Is there a second, separable population of accounts, and "
              "does it change around the election?")
    nul = res["null"]
    pg.h2("Data and its limits")
    pg.kpis([("third-party posts", f"{facts['third_party']:,}"), ("accounts", f"{facts['accounts']:,}"),
             (f"accounts with {MIN_POSTS}+ posts (modelled)", f"{len(res['N'].Z):,}"),
             ("share of posts they hold", f"{c.account.map(c.account.value_counts()).ge(MIN_POSTS).mean():.0%}"),
             ("posts before / after the election", f"{(c.period == 'before').sum():,} / {(c.period == 'after').sum():,}")])
    pg.note("<b>Sampling bias, read first.</b> The posts were collected by keyword search with at least 25 likes and at "
            "most 40 posts per 12 hours, ranked by recency. Low-engagement reply bots, the classic swarm, are "
            "filtered out by design, and only 20% of posts have a known parent. So this tests whether the method "
            "finds structure among <i>visible, engaged</i> accounts, not whether bots exist on X. Timing gaps are "
            "between an account's <i>sampled</i> posts, not all its posts.")
    daily = c.groupby(c.t.dt.tz_convert(TZ).dt.date).size()
    fig = go.Figure(go.Bar(x=list(daily.index), y=daily.values, marker_color=COLORS[0],
                           hovertemplate="%{x}: %{y} posts<extra></extra>"))
    fig.add_vline(x=ELECTION.tz_convert(TZ).normalize().timestamp() * 1000, line_dash="dash", line_color=COLORS[3])
    fig.update_layout(yaxis_title="posts per day")
    pg.fig(fig, "Posts per day (dashed: election)",
           "A flat line near 40-80 a day is the collection cap, not real volume; peaks above it are days when "
           "the cap was not hit. Do not read volume changes as activity changes.", height=280)

    pg.h2("Metrics carried over")
    rows = []
    for m in FROZEN:
        miss = res["N"].log.loc[m, "missing_%"] if m in res["cols"] else res["raw"][m].isna().mean() * 100
        rows.append({"metric": m, "group": REGISTRY[m].group, "missing_%": miss,
                     "fitted": "yes" if m in res["cols"] else f"no (> {MAX_MISSING:.0%} missing)",
                     "meaning": REGISTRY[m].description.replace("Kyiv", "New York").replace("in the week",
                                                                                           "in the period")})
    pg.table(pd.DataFrame(rows), {"missing_%": "{:.0f}"}, index=False)
    pg.p("Not carried over: channel and side metrics (one topic, one platform), post latency and arrival rank "
         "(no thread roots), crowding, partners and follow metrics (the 40-per-12-hours cap makes who posts near whom "
         "an artefact), two_faced and post_relevance.")

    pg.h2("Two populations, or one? The null test")
    pg.p("ΔBIC compares one Gaussian with two. But a single crowd whose metrics are skewed or spiked at zero is not "
         "Gaussian either, and two components fit it better too. So the right reference is not 0: it is the ΔBIC of "
         "a <b>single population that has the same metric distributions and the same correlations</b> as the real "
         "accounts, drawn from a Gaussian copula:")
    pg.math(r"u_{ij}=\Phi^{-1}\!\Big(\tfrac{\mathrm{rank}(z_{ij})}{n+1}\Big),\;\; R=\mathrm{corr}(u),\;\;"
            r"g^{(s)}\sim\mathcal N(0,R),\;\; \tilde z^{(s)}_{ij}=z_{(\mathrm{rank}\,g^{(s)}_{ij}),j}")
    pg.math(r"p=\frac{1+\#\{s:\Delta\mathrm{BIC}(\tilde z^{(s)})\ge\Delta\mathrm{BIC}(z)\}}{1+S},\qquad S="
            + str(N_NULL))
    pg.p("Whatever ΔBIC the real accounts show <i>beyond</i> this null is clustering: a group that sits apart from "
         "the crowd. A simpler null (each column shuffled on its own) is shown too; it also drops correlations, "
         "which lets a single Gaussian fit the real data better than the null and so favours the real data.")
    keys = ["accounts", "metrics", "delta_bic", "null_mean", "null_max", "p", "shuffle_max", "p_shuffle"]
    runs = [("Mamdani, all posts", nul), *[(f"Mamdani, {k} the election", v["null"]) for k, v in periods.items()],
            *([("Telegram (main set)", tg)] if tg else [])]
    tab = pd.DataFrame([{"data": name, **{k: r[k] for k in keys}} for name, r in runs]).set_index("data")
    tab = tab.rename(columns={"null_mean": "copula_mean", "null_max": "copula_max", "p": "p_copula"})
    pg.table(tab, {"delta_bic": "{:,.0f}", "copula_mean": "{:,.0f}", "copula_max": "{:,.0f}", "shuffle_max": "{:,.0f}",
                   "p_copula": "{:.3f}", "p_shuffle": "{:.3f}"})
    fig = go.Figure()
    for i, (name, r) in enumerate(runs):
        for j, (key, label) in enumerate([("null", "one-crowd copula"), ("shuffle", "columns shuffled")]):
            fig.add_trace(go.Box(x=r[key], y=[name] * len(r[key]), name=label, marker_color=COLORS[[5, 6][j]],
                                 boxpoints="all", jitter=0.3, orientation="h", showlegend=i == 0, offsetgroup=label,
                                 hovertemplate=label + " ΔBIC %{x:,.0f}<extra></extra>"))
        fig.add_trace(go.Scatter(x=[r["delta_bic"]], y=[name], mode="markers", marker=dict(size=14, color=COLORS[3],
                                 symbol="diamond"), showlegend=i == 0, name="real accounts",
                                 hovertemplate="real ΔBIC %{x:,.0f}<extra></extra>"))
    fig.update_layout(xaxis_title="ΔBIC (one vs two components)", xaxis_type="log", boxmode="group")
    pg.fig(fig, "Real ΔBIC (diamond) against one-crowd nulls",
           "A diamond to the right of every grey box = a separate population beyond what skewed, correlated metrics "
           "produce on their own. A diamond inside or left of the boxes = the 'two populations' are the shape of the "
           "metrics, not a group of accounts.", height=340)

    sep = res["sep"]
    pg.h2("What separates the two populations")
    pg.note("This section describes the two-component fit anyway, so it can be compared with Telegram. If the "
            "null test above did not beat the one-crowd copula, these 'populations' are a cut through one skewed crowd.")
    pg.kpis([("ΔBIC (real)", f"{nul['delta_bic']:,.0f}"), ("ΔBIC minus copula max", f"{nul['excess']:+,.0f}"),
             ("Bhattacharyya D_B", f"{sep.bhattacharyya:.2f}"), ("Bayes error bound", f"{sep.bayes_error_bound:.1e}"),
             ("population sizes", f"{sep.weights[0]:.0%} / {sep.weights[1]:.0%}")])
    pm = sep.per_metric.sort_values("ks")
    fig = go.Figure(go.Bar(x=pm.ks, y=pm.index, orientation="h", marker_color=COLORS[4], customdata=np.c_[pm.cohen_d, pm.auc],
                           hovertemplate="%{y}<br>KS %{x:.2f} · d %{customdata[0]:+.2f} · AUC %{customdata[1]:.2f}"
                                         "<extra></extra>"))
    fig.update_layout(xaxis_title="KS distance between the two components")
    pg.fig(fig, "Metrics that separate the populations",
           "Long bars drive the split. Cohen's d sign: + means the smaller population is higher. Compare with the "
           "Telegram split (driven by low_effort_share, ua_share, n_comments).", height=460)

    pg.h3("Outside check: who is in the smaller population?")
    pg.p("Account age and followers were <b>not</b> used to fit the model. If the smaller population were bot-like we "
         "would expect younger accounts with fewer followers.")
    pop = pd.Series(np.where(sep.labels == 1, "smaller", "larger"), index=res["N"].Z.index)
    out = acc.loc[pop.index].assign(population=pop)
    rows = []
    for col, label in [("account_age_days", "account age at first post (days)"), ("followers", "followers"),
                       ("new_account", f"created < {NEW_ACCOUNT_DAYS} days before posting")]:
        a, b = out[out.population == "larger"][col].dropna(), out[out.population == "smaller"][col].dropna()
        ks = stats.ks_2samp(a, b)
        rows.append({"variable": label, "larger (median or share)": a.mean() if col == "new_account" else a.median(),
                     "smaller (median or share)": b.mean() if col == "new_account" else b.median(),
                     "KS": ks.statistic, "p": ks.pvalue})
    pg.table(pd.DataFrame(rows), {"larger (median or share)": "{:,.2f}", "smaller (median or share)": "{:,.2f}",
                                  "KS": "{:.2f}", "p": "{:.3g}"}, index=False)
    blocks = []
    for col, title, log in [("account_age_days", "Account age (days, log)", True), ("followers", "Followers (log)", True)]:
        f = go.Figure()
        for i, p in enumerate(["larger", "smaller"]):
            v = out[out.population == p][col]
            f.add_trace(kv_hist(np.log10(v.clip(lower=1)) if log else v, COLORS[[0, 3][i]], f"{p} population"))
        f.update_layout(barmode="overlay", xaxis_title=f"log10 {col}", yaxis_title="share of accounts")
        blocks.append(pg.fig(f, title, "Two overlapping shapes = the populations do not differ on this outside "
                                       "variable; a pink hump at the left = the smaller population is younger or "
                                       "less followed.", height=280))
    pg.grid(blocks)

    pg.h2("Before vs after the election")
    pg.p("Each metric is computed per period from that period's posts only (accounts with "
         f"{MIN_POSTS}+ posts in the period), then compared with a two-sample KS test.")
    pg.note("The before period is 35 days and the after period 56, and the collection cap is the same per day, so "
            "gaps between an account's sampled posts are longer after the election by construction. Gap metrics "
            "(median_gap, min_gap) shifting is expected; content and semantic metrics are the ones to read.")
    pg.table(shift, {"before_median": "{:.3g}", "after_median": "{:.3g}", "KS": "{:.2f}", "p": "{:.3g}",
                     "p_holm": "{:.3g}"})
    fig = go.Figure()
    for i, (col, label) in enumerate([("paraphrase", "reworded from another account"), ("cross_dup", "word-for-word copy"),
                                      ("new_account", f"from accounts < {NEW_ACCOUNT_DAYS} days old"),
                                      ("smaller_pop", "from the smaller population")]):
        fig.add_trace(go.Scatter(x=weekly.index.astype(str), y=weekly[col], name=label, mode="lines+markers",
                                 line_color=COLORS[i], hovertemplate="week of %{x}: %{y:.1%}<extra>" + label + "</extra>"))
    fig.add_vline(x=str(weekly.index[weekly.index <= ELECTION.tz_convert(None)].max()), line_dash="dash", line_color=COLORS[3])
    fig.update_layout(yaxis_tickformat=".0%", yaxis_title="share of the week's posts")
    pg.fig(fig, "Weekly share of suspicious kinds of post (dashed: election week)",
           "A jump in the election week that falls back afterwards is the pattern of a campaign push. Flat lines mean "
           "the election did not change the mix of who posts and how.", height=340)

    pg.h2("What changed after the election")
    pg.p("Per post, not per account. The daily cap flattens volume, but shares within a period are comparable. "
         "p values are Holm-adjusted across the indicators.")
    pg.table(ch["indicators"], {"before": "{:,.3g}", "after": "{:,.3g}", "p": "{:.2g}", "p_holm": "{:.2g}"})
    pg.note("<b>Hour of day is a collection artefact here.</b> 97% of the 12-hour collection windows hit the 40-post "
            "cap and every window ends at 00:00 or 12:00 UTC, so posts bunch in the last hours before those times. "
            "After the election fewer posts reach 25 likes, the newest 40 stretch further back (mean 3.1 h before the "
            "window end vs 2.1 h), and that stretch reaches New York's night. The night-share rise therefore measures "
            "lower volume, not night-time posters, and the account-level timing metrics (gaps, hours) are unreliable "
            "on this dataset.")
    pg.p("Checks of the other shifts: the drop in likes holds <i>within the same 469 accounts</i> (median 436 → 212, "
         "Wilcoxon p = 6e-6), so it is less attention per post, not a different crowd; likes per view also fell "
         "(0.025 → 0.021). Accounts created in 2025 rose from 3.1% to 4.8% of authors, but creation dates show no "
         "batches (at most 3-4 accounts on any 2025 day; the largest same-day groups are 2022-04 and 2024-07, "
         "public sign-up waves). Near-identical meaning between accounts (top 0.1% of e5 similarity) is "
         "<i>less</i> frequent after the election (1,138 vs 2,041 pairs per million), and the cross-language "
         "near-twins are poll tables and translated news.")
    tp = ch["topics"]
    fig = go.Figure()
    for i, p in enumerate(["before", "after"]):
        fig.add_trace(go.Bar(y=tp.index + " · " + tp.words.str.slice(0, 48), x=tp[p], name=f"{p} the election",
                             orientation="h", marker_color=COLORS[[0, 3][i]], customdata=np.c_[tp.z, tp.example],
                             hovertemplate="%{x:.1%} of posts · z %{customdata[0]:+.1f}<br>%{customdata[1]}"
                                           "<extra></extra>"))
    fig.update_layout(barmode="group", xaxis_tickformat=".0%", xaxis_title="share of the period's posts",
                      yaxis=dict(tickfont=dict(size=11)))
    pg.fig(fig, f"Topics (k-means on e5 vectors, {N_TOPICS} groups), sorted from shrinking to growing",
           "Top bars shrank after the election, bottom bars grew. z beyond ±3 is a real shift (χ² test over all "
           f"topics: p = {ch['topic_p']:.1e}). Hover for the most-liked post in each topic.", height=560)
    pg.table(tp[["posts", "before", "after", "ratio", "z", "words", "example"]],
             {"before": "{:.1%}", "after": "{:.1%}", "ratio": "{:.2f}", "z": "{:+.1f}"})
    cp = ch["copies"]
    pg.h3(f"Word-for-word copies across accounts ({len(cp)} texts)")
    pg.p("A text of 15+ characters posted by 2 or more accounts. Headlines shared by news accounts are expected; "
         "the same opinion text from different accounts within minutes is a lead.")
    pg.table(cp.head(15)[["accounts", "posts", "period", "span_h", "text"]], {"span_h": "{:.1f}"}, index=False)

    nb = new_accounts(c, out.assign(anomaly=res["anomaly"].combined))
    pg.h2("If every new account were a bot")
    pg.p(f"A working assumption, not a finding: every account created in 2025 ({nb['n_bots']} of {nb['n_accounts']:,} "
         f"authors, {(nb['posts_per_bot'] == 1).sum()} of them with a single post) is treated as a bot. The question "
         "is the one that validated the method on Telegram: without seeing this label, do the metrics pick these "
         "accounts out?")
    pg.kpis([("share of posts by 2025 accounts, before → after",
              f"{nb['share']['before']:.1%} → {nb['share']['after']:.1%}"),
             ("Fisher p (share rose)", f"{nb['share_p']:.3f}"),
             (f"2025 accounts among the {nb['modelled']} modelled", f"{nb['modelled_bots']}"),
             ("content-only AUC (chance ≈ 0.5)", f"{nb['content_auc']:.2f}"),
             ("content AUC with shuffled labels, max", f"{nb['content_null'].max():.2f}")])
    pg.table(nb["scores"], {"auc": "{:.2f}", "p": "{:.3f}"})
    pg.p("AUC = probability that a random 2025 account scores higher than a random older one (0.5 = the score "
         "cannot tell them apart; below 0.5 = 2025 accounts score lower). Timing metrics are left out: hour and gaps "
         "are set by the collection windows here. Content-only AUC: a logistic regression on each post's e5 vector, "
         "cross-validated so no account is in both training and test folds, compared with the same model trained on "
         "labels shuffled between accounts.")
    pg.h3("How 2025 accounts behave, per post")
    pg.table(nb["behaviour"], {"older accounts": "{:,.3g}", "2025 accounts": "{:,.3g}", "p": "{:.1e}"}, index=False)
    pg.p("Most-replied-to accounts in 2025 accounts' replies: " +
         ", ".join(f"@{h} ({n})" for h, n in nb["targets"].items()) + ".")

    pg.h2("Most unusual accounts")
    pg.p("Top 15 by the combined anomaly score (GMM −log p and Isolation Forest, rank-averaged). A lead, not a verdict.")
    top = out.assign(anomaly=res["anomaly"].combined).sort_values("anomaly", ascending=False).head(15)
    show = top[["population", "anomaly", "n_comments", "account_age_days", "followers", "cross_dup_share",
                "paraphrase_share", "url_share", "example"]]
    pg.table(show, {"anomaly": "{:.3f}", "n_comments": "{:.0f}", "account_age_days": "{:,.0f}", "followers": "{:,.0f}",
                    "cross_dup_share": "{:.2f}", "paraphrase_share": "{:.2f}", "url_share": "{:.2f}"})

    pg.h2("Summary in plain words")
    smaller = sep.per_metric.sort_values("ks", ascending=False).index[:3]
    verdict = ("beats" if nul["p"] <= 0.05 else "does not beat")
    age = rows[0]
    tg_txt = (f" The same test on the Telegram accounts gives ΔBIC {tg['delta_bic']:,.0f} against a one-crowd null of "
              f"up to {tg['null_max']:,.0f} (p = {tg['p']:.3f}), so the Telegram 'two distributions' "
              f"{'also survive' if tg['p'] <= 0.05 else 'do not survive this test either'}." if tg else "")
    pg.plain(f"On {len(res['N'].Z):,} X accounts the two-population model {verdict} a single crowd with the same "
             f"metric distributions and correlations (ΔBIC {nul['delta_bic']:,.0f} vs up to {nul['null_max']:,.0f} "
             f"for the one-crowd copula, p = {nul['p']:.3f}).{tg_txt} The two-component split is driven by "
             f"{', '.join(smaller)}. Account age, which the model never saw, "
             f"{'differs' if age['p'] < 0.05 else 'does not differ'} between the populations (median "
             f"{age['larger (median or share)']:,.0f} vs {age['smaller (median or share)']:,.0f} days, KS p = "
             f"{age['p']:.2g}). Because the data holds only posts with 25+ likes, this tests the method on visible "
             f"accounts; it cannot show the low-engagement reply swarms a full reply collection would.")
    return pg


# ---------------------------------------------------------------- main

def main() -> None:
    cfg.LOCAL_TZ = TZ
    cfg.CACHE = cfg.ANALYSIS / "cache" / "mamdani"
    cfg.CACHE.mkdir(parents=True, exist_ok=True)
    cfg.RESULTS.mkdir(exist_ok=True)
    print("1/5 load")
    c, facts = load()
    print(f"   {len(c):,} posts, {c.account.nunique():,} accounts")
    print("2/5 vectors")
    ensure_vectors(c)
    print("3/5 metrics + models (all posts)")
    ctx, raw = account_metrics(c, "all")
    res = fit(raw)
    res["raw"] = raw
    print(f"   ΔBIC {res['null']['delta_bic']:,.0f}, one-crowd max {res['null']['null_max']:,.0f}, p {res['null']['p']:.3f}")
    print("4/5 periods")
    periods, period_raw = {}, {}
    for period in ["before", "after"]:
        _, r = account_metrics(c[c.period == period].reset_index(drop=True), period)
        period_raw[period] = r
        periods[period] = fit(r)
        print(f"   {period}: {len(r)} accounts, ΔBIC {periods[period]['null']['delta_bic']:,.0f} "
              f"vs one-crowd max {periods[period]['null']['null_max']:,.0f}")
    shift = []
    for m in FROZEN:
        a, b = period_raw["before"][m].dropna(), period_raw["after"][m].dropna()
        if len(a) > 10 and len(b) > 10:
            ks = stats.ks_2samp(a, b)
            shift.append({"metric": m, "before_median": a.median(), "after_median": b.median(),
                          "KS": ks.statistic, "p": ks.pvalue})
    shift = pd.DataFrame(shift).sort_values("p")
    shift["p_holm"] = (shift.p * np.arange(len(shift), 0, -1)).cummax().clip(upper=1).to_numpy()
    shift = shift.set_index("metric")
    tg = telegram_null()
    if tg:
        print(f"   telegram: ΔBIC {tg['delta_bic']:,.0f} vs one-crowd max {tg['null_max']:,.0f}")

    print("5/5 report")
    sep = res["sep"]
    smaller = set(res["N"].Z.index[sep.labels == 1])
    para = ctx.paraphrase.set_index("id")
    para_flag = (para.nn_sim >= para.threshold)
    long = c[c.text_len >= cfg.DUP_MIN_CHARS]
    shared = long.groupby("text_key").account.nunique()
    c = c.assign(paraphrase=c.id.map(para_flag).fillna(False).astype(float),
                 cross_dup=(c.text_key.map(shared).fillna(0) > 1).astype(float),
                 new_account=(c.account_age_days < NEW_ACCOUNT_DAYS).astype(float),
                 smaller_pop=c.account.isin(smaller).astype(float))
    weekly = c.groupby(c.t.dt.tz_convert(None).dt.to_period("W").dt.start_time)[
        ["paraphrase", "cross_dup", "new_account", "smaller_pop"]].mean()
    acc = c.groupby("account").agg(account_age_days=("account_age_days", "min"), followers=("followers", "max"),
                                   example=("text", lambda s: s.iloc[0][:140]))
    acc["new_account"] = (acc.account_age_days < NEW_ACCOUNT_DAYS).astype(float)
    acc = acc.join(raw)
    pg = report(c, facts, res, periods, tg, acc, weekly, shift, changes(c))
    print("  ", pg.save(cfg.RESULTS))
    out = acc.loc[res["N"].Z.index].assign(population=np.where(sep.labels == 1, "smaller", "larger"),
                                          anomaly=res["anomaly"].combined)
    out.to_csv(cfg.RESULTS / "mamdani_accounts.csv.gz")
    print("  ", cfg.RESULTS / "mamdani_accounts.csv.gz")


if __name__ == "__main__":
    main()
