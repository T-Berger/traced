"""The web: accounts linked by repeatedly saying the same thing at the same time.

event      two long comments by different accounts, posted within W seconds of each other, whose
           e5 cosine is in the top 1% of all such time-close pairs (per length decile of the shorter text)
           mode "post": both on the same root post · mode "any": anywhere (any post, channel or side)
occasion   what makes two events different: the post (mode "post") or the hour (mode "any")
           Not an event: one comment replies to the other, or both reply to the same comment. That is a
           conversation (regulars chatting fast and in the same style), not side-by-side amplification.
edge       two accounts with events on >= k distinct occasions spread over >= 2 days
           (one news wave on one day cannot create an edge), who do not talk to each other: pairs with
           >= TALK_REPLIES direct replies between them in the week are conversation partners and are
           reported separately, not linked.
test       each account's whole timeline is shifted by a random whole number of days, wrapping around
           the observed window, and the edges are rebuilt with the SAME similarity thresholds, N_SHUFFLES times. Shifting keeps every
           account's daily rhythm and wording but breaks synchrony with any specific partner, so shuffled
           edges are chance edges. Whole days matter: an hour shift (tried first) broke the time of day,
           so two people who both say "доброе утро" at 8 am looked coordinated.
           Per pair: an edge (>= MIN_OCCASIONS occasions over >= MIN_DAYS days) is significant only if NO
           shifted timeline gives that pair as many occasions (p < 1/(N+1)). Busy pairs that overlap a
           lot by chance thus face a higher bar than quiet ones; a single global cut-off could not
           separate them (tried: chance edges stayed at 50-60% of real ones for every k).
           Expected false edges: treat each shuffle as if real, test it against the others, average.
webs       Louvain communities on the significant edges, weight = occasions.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import networkx as nx
import numpy as np
import pandas as pd

from . import config as cfg

MODES = {"post": "same root post", "any": "anywhere (any post, channel or side)"}
TOP_Q = 0.99
N_SHUFFLES = 20
K_RANGE = range(3, 13)
MIN_OCCASIONS = 3
MIN_DAYS = 2
TALK_REPLIES = 3


@dataclass
class Web:
    mode: str
    n_close_pairs: int
    thresholds: pd.Series  # per length decile
    events: pd.DataFrame  # real events (i, j rows into the long-text table, sim, dt, occasion, day)
    k_table: pd.DataFrame  # k -> real edges, mean shuffled edges, ratio (global view)
    tested: int  # candidate edges (>= MIN_OCCASIONS over >= MIN_DAYS)
    expected_false: float  # leave-one-shuffle-out estimate of chance edges among the significant ones
    edges: pd.DataFrame  # significant edges: a, b, occasions, days, events, null_max
    conversation_edges: int = 0  # would-be edges dropped because the pair talks to each other
    conversation_events: int = 0  # time-close similar pairs dropped as replies within a thread
    communities: list[set] = field(default_factory=list)


def _close_pairs(t: np.ndarray, acc: np.ndarray, root: np.ndarray, X: np.ndarray, mode: str,
                 window: float, block: int = 2048):
    """All pairs (i, j) of different accounts within `window` seconds (same root in mode 'post'),
    with their cosine. Block-wise dense products on the time-sorted rows: one BLAS call per block."""
    key = t.astype(np.float64) if mode == "any" else root.astype(np.float64) * 1e7 + t
    order = np.argsort(key, kind="stable")
    key, Xo, acc_o = key[order], X[order], acc[order]
    ends = np.searchsorted(key, key + window, side="right")
    rows_i, cols_j, sims = [], [], []
    for a in range(0, len(key), block):
        b = min(a + block, len(key))
        e = ends[b - 1]
        rows = np.arange(a, b)[:, None]
        cols = np.arange(a, e)[None, :]
        valid = (cols > rows) & (cols < ends[a:b, None]) & (acc_o[a:b, None] != acc_o[None, a:e])
        if not valid.any():
            continue
        sim = Xo[a:b] @ Xo[a:e].T
        ii, jj = np.nonzero(valid)
        rows_i.append(order[a + ii])
        cols_j.append(order[a + jj])
        sims.append(sim[ii, jj].astype(np.float32))
    return np.concatenate(rows_i), np.concatenate(cols_j), np.concatenate(sims)


def _decile(length: np.ndarray, edges: np.ndarray) -> np.ndarray:
    return np.clip(np.searchsorted(edges, length, side="right") - 1, 0, len(edges) - 2)


def _events(m: pd.DataFrame, t, i, j, sim, thr: pd.Series, len_edges, mode) -> pd.DataFrame:
    """Time-close pairs above the similarity threshold, minus replies inside one conversation."""
    short = np.minimum(m.text_len.to_numpy()[i], m.text_len.to_numpy()[j])
    hit = sim >= thr.reindex(_decile(short, len_edges)).to_numpy()
    i, j, sim = i[hit], j[hit], sim[hit]
    ids, par = m.id.to_numpy(), m.parent_id.to_numpy()
    talk = (par[i] == ids[j]) | (par[j] == ids[i]) | ((par[i] == par[j]) & m.parent_is_comment.to_numpy()[i])
    i, j, sim = i[~talk], j[~talk], sim[~talk]
    first = np.where(t[i] <= t[j], i, j)
    occ = m.root_id.to_numpy()[i] if mode == "post" else (t[first] // 3600).astype(np.int64)
    acc = m.account.to_numpy()
    return pd.DataFrame({"i": i, "j": j, "sim": sim, "dt": np.abs(t[i] - t[j]),
                         "a": np.minimum(acc[i], acc[j]), "b": np.maximum(acc[i], acc[j]),
                         "leader": acc[first], "occasion": occ, "day": t[first] // 86400})


def _edges(ev: pd.DataFrame) -> pd.DataFrame:
    if ev.empty:
        return pd.DataFrame(columns=["a", "b", "occasions", "days", "events"])
    g = ev.groupby(["a", "b"])
    return pd.DataFrame({"occasions": g.occasion.nunique(), "days": g.day.nunique(),
                         "events": g.size()}).reset_index()


def _count(edges: pd.DataFrame, k: int) -> int:
    return int(((edges.occasions >= k) & (edges.days >= MIN_DAYS)).sum())


def build(m: pd.DataFrame, X: np.ndarray, mode: str, talk_pairs: set) -> Web:
    """m: long-text table (id, account, root_id, parent_id, parent_is_comment, text_len, t);
    X: centred vectors, rows aligned; talk_pairs: (a, b) account pairs that reply to each other."""
    t = (m.t - m.t.min()).dt.total_seconds().to_numpy()
    acc = pd.factorize(m.account)[0]
    root = m.root_id.fillna(-1).astype("int64").to_numpy()
    if mode == "post":  # orphans have no post: give each its own key so they never pair
        root = np.where(root < 0, -np.arange(1, len(root) + 1), root)
    W = cfg.COACTIVITY_WINDOW_S

    i, j, sim = _close_pairs(t, acc, root, X, mode, W)
    short = np.minimum(m.text_len.to_numpy()[i], m.text_len.to_numpy()[j])
    len_edges = np.unique(np.quantile(m.text_len, np.linspace(0, 1, 11)))
    thr = pd.Series(sim).groupby(_decile(short, len_edges)).quantile(TOP_Q)
    ev = _events(m, t, i, j, sim, thr, len_edges, mode)
    n_talk_events = int((sim >= thr.reindex(_decile(short, len_edges)).to_numpy()).sum() - len(ev))
    real = _edges(ev)
    is_talk = np.array([(a, b) in talk_pairs for a, b in zip(real.a, real.b, strict=True)], dtype=bool)
    n_talk_edges = _count(real[is_talk], MIN_OCCASIONS)
    real = real[~is_talk]
    ev = ev[~pd.MultiIndex.from_frame(ev[["a", "b"]]).isin(list(talk_pairs))] if talk_pairs else ev

    rng = np.random.default_rng(cfg.SEED)
    n_days = int(np.ceil((t.max() + 1) / 86400))
    shuffled = []
    for _ in range(N_SHUFFLES):
        shift = rng.integers(1, n_days, acc.max() + 1) * 86400.0
        ts = (t + shift[acc]) % (n_days * 86400.0)
        si, sj, ss = _close_pairs(ts, acc, root, X, mode, W)
        e = _edges(_events(m, ts, si, sj, ss, thr, len_edges, mode))
        keep = np.array([(a, b) not in talk_pairs for a, b in zip(e.a, e.b, strict=True)], dtype=bool)
        shuffled.append(e[keep] if len(e) else e)

    rows = []
    for k in K_RANGE:
        r = _count(real, k)
        sh = float(np.mean([_count(e, k) for e in shuffled]))
        rows.append({"k": k, "real_edges": r, "shuffled_edges": sh, "ratio": sh / r if r else np.nan})
    kt = pd.DataFrame(rows).set_index("k")

    def level(e: pd.DataFrame) -> pd.Series:
        e = e[(e.occasions >= MIN_OCCASIONS) & (e.days >= MIN_DAYS)]
        return pd.Series(e.occasions.to_numpy(), index=pd.MultiIndex.from_frame(e[["a", "b"]]), dtype=float)

    levels = [level(e) for e in shuffled]

    def passing(cand: pd.Series, nulls: list[pd.Series]) -> pd.Series:
        best = pd.concat([n.reindex(cand.index).fillna(0) for n in nulls], axis=1).max(axis=1) if nulls else 0
        return best

    cand = level(real)
    null_max = passing(cand, levels)
    sig_idx = cand.index[cand > null_max]
    false = [int((lv > passing(lv, levels[:s] + levels[s + 1:])).sum()) for s, lv in enumerate(levels)]
    sig = real.set_index(["a", "b"]).loc[sig_idx].assign(null_max=null_max[sig_idx]).reset_index()

    G = nx.Graph()
    G.add_weighted_edges_from(sig[["a", "b", "occasions"]].itertuples(index=False))
    comms = [c for c in nx.community.louvain_communities(G, weight="weight", seed=cfg.SEED) if len(c) >= 3] \
        if len(G) else []
    comms.sort(key=len, reverse=True)
    return Web(mode, len(sim), thr, ev, kt, len(cand), float(np.mean(false)), sig, conversation_edges=n_talk_edges,
               conversation_events=n_talk_events, communities=comms)


def talk_pairs(comments: pd.DataFrame, min_replies: int = TALK_REPLIES) -> set:
    """Account pairs (sorted) with >= min_replies direct replies between them, either direction."""
    owner = comments.set_index("id").account
    d = comments.dropna(subset=["parent_id"])
    to = d.parent_id.astype("int64").map(owner)
    ok = to.notna() & (to != d.account)
    a, b = d.account[ok].to_numpy(), to[ok].to_numpy()
    pairs = pd.DataFrame({"a": np.minimum(a, b), "b": np.maximum(a, b)}).value_counts()
    return set(pairs[pairs >= min_replies].index)


def summary(web: Web, m: pd.DataFrame, X: np.ndarray, comments: pd.DataFrame) -> pd.DataFrame:
    """One row per web: who, where, which side, how often, who leads, what they say."""
    text = comments.set_index("id").text
    rows = []
    for n, members in enumerate(web.communities):
        e = web.edges[web.edges.a.isin(members) & web.edges.b.isin(members)]
        ev = web.events[web.events.a.isin(members) & web.events.b.isin(members)]
        own = m.account.isin(members).to_numpy()
        mc = comments[comments.account.isin(members)]
        rows_ev = np.unique(np.r_[ev.i.to_numpy(), ev.j.to_numpy()])
        cen = X[rows_ev].mean(axis=0) if len(rows_ev) else X[own].mean(axis=0)
        pool = rows_ev if len(rows_ev) else np.where(own)[0]
        central = pool[np.argsort(-(X[pool] @ cen))[:3]]
        leads = ev.leader.value_counts()
        rows.append({
            "web": n, "accounts": len(members), "edges": len(e), "events": len(ev),
            "occasions": int(ev.occasion.nunique()), "days": int(ev.day.nunique()),
            "channels": ", ".join(mc.channel.value_counts().index[:3]),
            "ua_share": float((mc.side == "ua").mean()),
            "leader": f"{leads.index[0][:30]} ({leads.iloc[0] / len(ev):.0%})" if len(ev) else "",
            "median_dt_s": float(ev.dt.median()) if len(ev) else np.nan,
            "members": ", ".join(sorted(x[:24] for x in members)[:8]) + (" …" if len(members) > 8 else ""),
            "central_messages": " ⟂ ".join(str(text[mid])[:140].replace("\n", " ") for mid in m.id.to_numpy()[central]),
        })
    cols = ["web", "accounts", "edges", "events", "occasions", "days", "channels", "ua_share", "leader",
            "median_dt_s", "members", "central_messages"]
    return pd.DataFrame(rows, columns=cols)


def degree(web: Web) -> pd.Series:
    return pd.concat([web.edges.a, web.edges.b]).value_counts()


def membership(web: Web) -> pd.Series:
    return pd.Series({a: n for n, c in enumerate(web.communities) for a in c}, dtype="Int64")


def sweep(m: pd.DataFrame, X: np.ndarray, talk: set, n_shuffles: int = 10) -> pd.DataFrame:
    """Robustness: the whole test under stricter similarity cuts and shorter windows."""
    saved = (TOP_Q, N_SHUFFLES, cfg.COACTIVITY_WINDOW_S)
    rows = []
    try:
        for q in (0.99, 0.999):
            for w in (60, 300):
                for mode in MODES:
                    globals()["TOP_Q"], globals()["N_SHUFFLES"], cfg.COACTIVITY_WINDOW_S = q, n_shuffles, w
                    r = build(m, X, mode, talk)
                    k3 = r.k_table.loc[MIN_OCCASIONS]
                    rows.append({"similarity cut": f"top {1 - q:.1%}", "window_s": w, "mode": mode,
                                 "events": len(r.events), "edges (real)": int(k3.real_edges),
                                 "edges (chance)": float(k3.shuffled_edges), "significant pairs": len(r.edges),
                                 "expected false": r.expected_false, "webs": len(r.communities)})
    finally:
        globals()["TOP_Q"], globals()["N_SHUFFLES"], cfg.COACTIVITY_WINDOW_S = saved
    return pd.DataFrame(rows)
