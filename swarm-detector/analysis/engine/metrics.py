"""Account-level metrics as plug-ins.

Adding a metric is one decorated function: it receives the context and returns one value per
account (a Series indexed by account). Everything downstream (normalisation, models, importance,
separability, reports) picks it up from the registry, e.g.

    @metric("timing", transform="log1p",
            description="Median seconds from the parent message to the reply.",
            hypothesis="Scripted accounts answer on a trigger, so their reply delay is short and steady.",
            look_for="A separate low mode: a group that always answers within seconds.")
    def median_parent_latency(ctx):
        return ctx.c.groupby("account").latency_parent_s.median()
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from functools import cached_property

import numpy as np
import pandas as pd

from . import coactivity, embeddings, semantic
from . import config as cfg


@dataclass(frozen=True)
class Metric:
    name: str
    group: str
    func: Callable
    transform: str  # "none" | "log1p" | "logit"
    description: str
    hypothesis: str
    look_for: str


REGISTRY: dict[str, Metric] = {}


def metric(group: str, transform: str = "none", description: str = "", hypothesis: str = "",
           look_for: str = ""):
    def register(func):
        REGISTRY[func.__name__] = Metric(func.__name__, group, func, transform, description, hypothesis, look_for)
        return func
    return register


class Ctx:
    """Shared inputs. Expensive derived tables are cached properties, built once on first use."""

    def __init__(self, comments: pd.DataFrame):
        self.all = comments
        self.c = comments[comments.in_model]
        self.accounts = pd.Index(sorted(self.c.account.unique()), name="account")
        self.g = self.c.groupby("account")

    @cached_property
    def text(self) -> pd.DataFrame:
        return self.c[~self.c.is_media_only]

    @cached_property
    def pairs(self) -> pd.DataFrame:
        return coactivity.pairs(self.all, cfg.COACTIVITY_WINDOW_S)

    @cached_property
    def roots_per_account(self) -> pd.Series:
        return self.all.dropna(subset=["root_id"]).groupby("account").root_id.nunique()

    @cached_property
    def crowding(self) -> pd.Series:
        return coactivity.crowding(self.all, 30)

    # ---- semantic inputs (stored vectors; see engine/embeddings.py)

    def _vectors(self, kind: str) -> tuple[pd.DataFrame, np.ndarray]:
        """Comment table aligned with the centred vectors of `kind` for the configured model."""
        mask = embeddings.embeddable(self.all)
        v = embeddings.load(cfg.EMBED_MODEL, kind, self.all.id[mask])
        meta = self.all.set_index("id").loc[v.ids, ["account", "root_id", "text_key", "text_len"]]
        return meta.reset_index(), embeddings.centred(v.X)

    @cached_property
    def sem(self) -> tuple[pd.DataFrame, np.ndarray]:
        return self._vectors(cfg.EMBED_KIND)

    @cached_property
    def paraphrase(self) -> pd.DataFrame:
        """Long texts with their best match from another account (exact copies excluded) and the
        chance threshold. Cached on disk per model and kind: the exact search takes a minute or two."""
        meta, X = self.sem
        keep = (meta.text_len >= cfg.DUP_MIN_CHARS).to_numpy()
        m, Xl = meta[keep].reset_index(drop=True), X[keep]
        acc = pd.factorize(m.account)[0]
        txt = pd.factorize(m.text_key)[0]
        path = cfg.CACHE / f"nn_{cfg.EMBED_MODEL}_{cfg.EMBED_KIND}_{embeddings.fingerprint(m.id)}.npy"
        if path.exists():
            nn = np.load(path)
        else:
            nn = semantic.nearest_other(Xl, acc, txt)
            np.save(path, nn)
        return m.assign(nn_sim=nn, threshold=semantic.echo_threshold(nn, m.text_len.to_numpy(), cfg.PARAPHRASE_Q))

    @cached_property
    def follows(self) -> pd.DataFrame:
        """Per comment: the other account directly above it on the same post, and the delay."""
        return coactivity.follows(self.all)

    @cached_property
    def follow_pairs(self) -> pd.DataFrame:
        """follower -> leader counts and median delay: input for the future swarm graph."""
        f = self.follows.dropna(subset=["leader"])
        return (f.groupby(["account", "leader"]).delta_s.agg(["size", "median"])
                .rename(columns={"size": "n", "median": "median_delta_s"}).reset_index()
                .rename(columns={"account": "follower"}).sort_values("n", ascending=False))


def compute(ctx: Ctx, names: list[str] | None = None) -> pd.DataFrame:
    cols = {}
    for name in names or REGISTRY:
        cols[name] = REGISTRY[name].func(ctx).reindex(ctx.accounts).astype(float)
    return pd.DataFrame(cols, index=ctx.accounts)


def _entropy(counts: pd.DataFrame) -> pd.Series:
    p = counts.div(counts.sum(axis=1), axis=0)
    return -(p * np.log2(p.where(p > 0, 1))).sum(axis=1)


# ---------------------------------------------------------------- activity

@metric("activity", "log1p",
        "Number of comments in the week.",
        "Swarm accounts are either very busy (amplifiers) or thin (throwaways); volume alone proves nothing.",
        "A heavy right tail is normal (power users). Use it as context, never as a verdict.")
def n_comments(ctx):
    return ctx.g.size()


@metric("activity", "none",
        "Distinct local (Kyiv) calendar days with at least one comment.",
        "Persistent operators post every day; event-driven humans cluster around news.",
        "Two humps (1-2 days vs all days) suggest different populations.")
def active_days(ctx):
    return ctx.c.assign(day=ctx.c.t.dt.tz_convert(cfg.LOCAL_TZ).dt.date).groupby("account").day.nunique()


# ---------------------------------------------------------------- breadth

@metric("breadth", "log1p",
        "Number of distinct channels commented in.",
        "Narrative carriers spread across channels; most readers stick to one or two.",
        "Accounts far right of the bulk are worth a look, especially if they also cross sides.")
def n_channels(ctx):
    return ctx.g.channel.nunique()


@metric("breadth", "none",
        "Shannon entropy of the account's comments over channels, in bits.",
        "Spread evenly across many channels = high; one home channel = 0.",
        "A mode near 0 (loyal readers) and a second group well above 1 bit.")
def channel_entropy(ctx):
    return _entropy(ctx.c.groupby(["account", "channel"]).size().unstack(fill_value=0))


@metric("breadth", "none",
        "Share of comments in Ukrainian-side channels (0 = only pro-Kremlin, 1 = only Ukrainian).",
        "Context for the brief's priority: pro-Kremlin narratives carried into Ukrainian debates.",
        "Two spikes at 0 and 1 are expected (home audiences). Mass in between is the cross-side population.")
def ua_share(ctx):
    return ctx.g.side.apply(lambda s: (s == "ua").mean())


@metric("breadth", "none",
        "2 x min(UA share, RU share): 0 = one side only, 1 = perfectly split between sides.",
        "The brief prioritises accounts active on both sides.",
        "Most accounts sit at 0. A cluster above ~0.2 is the cross-side group to inspect.")
def both_sides(ctx):
    ua = ctx.g.side.apply(lambda s: (s == "ua").mean())
    return 2 * np.minimum(ua, 1 - ua)


# ---------------------------------------------------------------- timing

@metric("timing", "log1p",
        "Median seconds from the root post to the account's comment.",
        "Accounts primed to react (alerts, scripts) comment within the first minutes.",
        "A low mode (seconds to a minute) separate from the human bulk (tens of minutes to hours).")
def median_post_latency(ctx):
    return ctx.g.latency_post_s.median()


@metric("timing", "none",
        f"Share of comments posted within {cfg.FAST_REPLY_S} s of the root post.",
        "Coordinated pushes land in the first minutes, before organic discussion.",
        "A group with share near 1 while most accounts are near 0.")
def fast_reply_share(ctx):
    return ctx.c.assign(f=ctx.c.latency_post_s <= cfg.FAST_REPLY_S).groupby("account").f.mean()


@metric("timing", "log1p",
        "Median seconds between the account's consecutive comments.",
        "Very short, steady gaps suggest automation or shift work.",
        "Bulk around 10^3-10^4 s; a separate mode below 60 s.")
def median_gap(ctx):
    return ctx.g.gap_prev_s.median()


@metric("timing", "none",
        "Burstiness B = (sd - mean)/(sd + mean) of gaps: -1 clockwork, 0 random (Poisson), +1 bursty.",
        "Humans are bursty (B > 0). Schedulers produce regular gaps (B < 0).",
        "Accounts below 0 are unusual for humans.")
def burstiness(ctx):
    s = ctx.g.gap_prev_s.agg(["mean", "std"])
    return (s["std"] - s["mean"]) / (s["std"] + s["mean"])


@metric("timing", "none",
        "Entropy of the 24 local-hour histogram, divided by log2(24): 0 = one hour only, 1 = flat around the clock.",
        "Humans sleep; round-the-clock posting points to shifts or automation.",
        "A group near 1 with sizeable volume.")
def hour_entropy(ctx):
    return _entropy(ctx.c.groupby(["account", "hour"]).size().unstack(fill_value=0)) / np.log2(24)


@metric("timing", "none",
        "Share of comments posted 01:00-05:59 Kyiv time.",
        "Night activity at volume is unusual for a local human audience (or reveals another time zone).",
        "A tail of accounts with share > 0.3.")
def night_share(ctx):
    return ctx.c.assign(n=ctx.c.hour.isin(cfg.NIGHT_HOURS)).groupby("account").n.mean()


# ---------------------------------------------------------------- content

@metric("content", "log1p",
        "Mean normalised text length (characters, emoji and links removed), text comments only.",
        "Templates and slogans are short and uniform; discussion varies.",
        "Mostly context: combine with duplicate metrics.")
def mean_text_len(ctx):
    return ctx.text.groupby("account").text_len.mean()


@metric("content", "none",
        f"Share of the account's texts ({cfg.DUP_MIN_CHARS}+ chars) that another account also posted word for word.",
        "Copy-paste across accounts is the classic swarm fingerprint.",
        "Almost everyone is at 0. Any account clearly above 0 is a lead; a group of them is a swarm candidate.")
def cross_dup_share(ctx):
    t = ctx.all[ctx.all.text_len >= cfg.DUP_MIN_CHARS]
    shared = t.groupby("text_key").account.nunique()
    mine = ctx.text[ctx.text.text_len >= cfg.DUP_MIN_CHARS]
    return mine.assign(d=mine.text_key.map(shared) > 1).groupby("account").d.mean()


@metric("content", "none",
        f"Share of the account's texts ({cfg.DUP_MIN_CHARS}+ chars) that repeat one of its own earlier texts.",
        "Spammers and scripted accounts repeat themselves; humans rarely do word for word.",
        "A tail above ~0.2.")
def self_dup_share(ctx):
    mine = ctx.text[ctx.text.text_len >= cfg.DUP_MIN_CHARS]
    return mine.assign(d=mine.duplicated(["account", "text_key"])).groupby("account").d.mean()


@metric("content", "none",
        "Share of comments containing a link.",
        "Link pushing (own channels, fake news sites) is a common swarm task.",
        "A group with high share while the bulk is near 0.")
def url_share(ctx):
    return ctx.c.assign(u=ctx.c.n_urls > 0).groupby("account").u.mean()


@metric("content", "none",
        "Share of comments that are only emoji, '+', '?' or a sticker/media without text.",
        "Cheap engagement (reaction farming) needs no text.",
        "A group near 1.")
def low_effort_share(ctx):
    return ctx.c.assign(e=ctx.c.is_low_info | ctx.c.is_media_only).groupby("account").e.mean()


# ---------------------------------------------------------------- interaction

@metric("interaction", "none",
        "Share of comments that reply to another comment rather than to the post.",
        "Swarms either broadcast (reply to the post) or pile on (reply to a target comment).",
        "Two populations: broadcasters near 0, debaters near 1.")
def reply_share(ctx):
    return ctx.c.assign(r=ctx.c.parent_type == "SOCIAL_MEDIA_COMMENT").groupby("account").r.mean()


@metric("interaction", "log1p",
        "Mean number of other accounts' comments within +/-30 s on the same post.",
        "Swarms arrive together, so each of their comments has company within seconds.",
        "A group with consistently crowded comments, beyond what busy posts explain.")
def crowding_30s(ctx):
    cr = ctx.c.id.map(ctx.crowding)
    return cr.groupby(ctx.c.account).mean()


@metric("interaction", "none",
        f"Highest Jaccard overlap with any single other account: posts both commented on within "
        f"{cfg.COACTIVITY_WINDOW_S} s of each other / posts either commented on.",
        "Swarm members keep showing up together; Jaccard discounts partners that are simply everywhere.",
        "Values near 0 for almost everyone; pairs that co-occur on a large share of their posts stand out.")
def max_partner_jaccard(ctx):
    p, r = ctx.pairs, ctx.roots_per_account
    jac = p.shared_posts / (p.a.map(r) + p.b.map(r) - p.shared_posts)
    both = pd.concat([pd.Series(jac.values, index=p.a.values), pd.Series(jac.values, index=p.b.values)])
    return both.groupby(level=0).max().reindex(ctx.accounts, fill_value=0)


@metric("interaction", "log1p",
        f"Number of other accounts met on 3+ different posts within {cfg.COACTIVITY_WINDOW_S} s.",
        "A swarm member has several repeat partners; a lone reader has none or one.",
        "A tail of accounts with many repeat partners; check whether they are just hyperactive.")
def repeat_partners(ctx):
    p = ctx.pairs[ctx.pairs.shared_posts >= 3]
    return pd.concat([p.a, p.b]).value_counts().reindex(ctx.accounts, fill_value=0)


# ---------------------------------------------------------------- timing, round 2

@metric("timing", "log1p",
        "Circular standard deviation, in hours, of the account's first-comment time on each local day "
        "(needs 3+ active days).",
        "A human's routine drifts from day to day; a scheduler or a shift starts at the same time.",
        "A low mode near 0 h: accounts that start at the same minute every day.")
def routine_drift(ctx):
    local = ctx.c.t.dt.tz_convert(cfg.LOCAL_TZ)
    first = (ctx.c.assign(day=local.dt.date, h=local.dt.hour + local.dt.minute / 60)
             .groupby(["account", "day"]).h.min())
    theta = 2 * np.pi * first / 24
    z = pd.DataFrame({"c": np.cos(theta), "s": np.sin(theta)}).groupby(level="account")
    m = z.mean()
    r = np.hypot(m.c, m.s).clip(upper=1)
    sd_h = np.sqrt(-2 * np.log(r.where(r > 0))) * 24 / (2 * np.pi)
    return sd_h.where(z.size() >= 3)


@metric("timing", "none",
        "Median arrival rank on the post: 0 = first comment under the post, 1 = last.",
        "Latency in seconds depends on how busy a post is; rank does not. Pushers are among the first.",
        "A group near 0 separate from the bulk around 0.5; compare with median_post_latency.")
def arrival_rank(ctx):
    d = ctx.all.dropna(subset=["root_id"])
    n = d.groupby("root_id").id.transform("size")
    rank = (d.groupby("root_id").t.rank(method="first") - 1) / (n - 1)
    return rank.where(n > 1).groupby(d.account).median()


@metric("timing", "log1p",
        "5th-percentile seconds between the account's own consecutive comments.",
        "Even a fast human needs time to read and type; very short minimum gaps suggest automation.",
        "A tail at only a few seconds.")
def min_gap(ctx):
    return ctx.g.gap_prev_s.quantile(0.05)


@metric("timing", "log1p",
        "Median seconds between own consecutive comments that land in DIFFERENT channels.",
        "Switching channels takes a human time; a coordinated account hops channels within seconds.",
        "Missing for single-channel accounts (most); among the rest, a mode at seconds to a minute.")
def channel_switch_gap(ctx):
    d = ctx.c.sort_values(["account", "t"])
    switch = d.channel.ne(d.groupby("account").channel.shift()) & d.gap_prev_s.notna()
    return d[switch].groupby("account").gap_prev_s.median()


# ---------------------------------------------------------------- interaction, round 2

@metric("interaction", "log1p",
        "Median delay after the comment directly above it on the same post (by another account), divided by "
        "that post's median delay.",
        "Swarm members answer each other on cue, so they follow faster than the post's normal pace.",
        "A group well below 1 (faster than the post's rhythm); 1 = typical pace.")
def follow_gap(ctx):
    f = ctx.follows
    f = f[f.account.isin(ctx.accounts)]
    return f.groupby("account").delta_norm.median()


@metric("interaction", "none",
        "Share of comments posted under 10 s after another account's comment on the same post.",
        "Ten seconds is too short to read and reply; repeated sub-10 s follows point to coordination.",
        "Most accounts near 0; a group with a clear share is a lead, especially with repeat partners.")
def follow_fast_share(ctx):
    f = ctx.follows.dropna(subset=["delta_s"])
    f = f[f.account.isin(ctx.accounts)]
    return (f.delta_s < 10).groupby(f.account).mean()


# ---------------------------------------------------------------- semantic (stored embeddings)

@metric("semantic", "none",
        f"Share of the account's texts ({cfg.DUP_MIN_CHARS}+ chars) whose closest match from another account is "
        f"in the top {1 - cfg.PARAPHRASE_Q:.0%} of all texts of similar length: said again in other words. "
        "Exact copies are excluded (cross_dup_share counts those).",
        "Swarms reword a shared script so exact-match filters miss it; the meaning still lines up.",
        "Near 0 for most; a group with a clear share is a lead, more so if those accounts are also repeat partners.")
def paraphrase_share(ctx):
    p = ctx.paraphrase
    return (p.nn_sim >= p.threshold).groupby(p.account).mean()


@metric("semantic", "none",
        "Mean cosine similarity between the account's own comments (centred vectors).",
        "A scripted account keeps saying the same thing in different words; people wander between topics.",
        "A tail of accounts far above the bulk: single-message accounts.")
def self_similarity(ctx):
    meta, X = ctx.sem
    return semantic.self_similarity(X, meta.account)


@metric("semantic", "none",
        "Mean cosine between the account's comments (content vectors) and the post they sit under.",
        "Pasted talking points are often off-topic for the post; replies to the post are on-topic.",
        "A low group: accounts whose comments ignore what the post is about.")
def post_relevance(ctx):
    mask = embeddings.embeddable(ctx.all)
    vc = embeddings.load(cfg.EMBED_MODEL, "content", ctx.all.id[mask])
    vp = embeddings.load(cfg.EMBED_MODEL, "posts")
    mu = vc.X.mean(axis=0)
    C = vc.X - mu
    P = vp.X - mu
    C /= np.linalg.norm(C, axis=1, keepdims=True).clip(min=1e-9)
    P /= np.linalg.norm(P, axis=1, keepdims=True).clip(min=1e-9)
    meta = ctx.all.set_index("id").loc[vc.ids, ["account", "root_id"]]
    prow = vp.rows(meta.root_id.fillna(-1).astype("int64"))
    ok = prow >= 0
    sims = np.einsum("ij,ij->i", C[ok], P[prow[ok]])
    return pd.Series(sims).groupby(meta.account.to_numpy()[ok]).mean()


@metric("semantic", "none",
        "Mean cosine between the account's average message and those of its repeat partners "
        f"(accounts met on 3+ posts within {cfg.COACTIVITY_WINDOW_S} s).",
        "Accounts that show up together AND say the same thing are acting together; co-timing alone can be "
        "a shared interest.",
        "High values only matter where repeat_partners > 0; that combination is the core swarm signature.")
def partner_semantic_sim(ctx):
    meta, X = ctx.sem
    C = semantic.centroids(X, meta.account).drop(columns="_n")
    p = ctx.pairs[(ctx.pairs.shared_posts >= 3) & ctx.pairs.a.isin(C.index) & ctx.pairs.b.isin(C.index)]
    sim = np.einsum("ij,ij->i", C.loc[p.a].to_numpy(), C.loc[p.b].to_numpy())
    both = pd.concat([pd.Series(sim, index=p.a.values), pd.Series(sim, index=p.b.values)])
    return both.groupby(level=0).mean()
