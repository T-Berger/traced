"""Run the whole theory pipeline and write the three reports to results/.

    uvx uv@0.11.3 run --locked --package swarm-detector --group analysis \
        python swarm-detector/analysis/run.py [--fresh]

1 preprocessing  -> results/01_preprocessing.html
2 models         -> results/02_models.html
3 metric scores  -> results/03_metrics.html

Preprocessing and raw metrics are cached in cache/ (git-ignored); --fresh rebuilds them.
"""

from __future__ import annotations

import pickle
import sys
import warnings

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from engine import config as cfg
from engine import evaluate, features, metrics, models, preprocess
from engine.evaluate import CRITERIA
from engine.metrics import REGISTRY
from engine.page import COLORS, Page, esc
from plotly.subplots import make_subplots
from sklearn.metrics import adjusted_rand_score

warnings.filterwarnings("ignore", category=FutureWarning)

GROUP_COLOR = {"activity": COLORS[5], "breadth": COLORS[0], "timing": COLORS[1], "content": COLORS[2],
               "interaction": COLORS[3]}


def cached(name: str, build, fresh: bool):
    path = cfg.CACHE / f"{name}.pkl"
    if path.exists() and not fresh:
        return pickle.loads(path.read_bytes())
    obj = build()
    path.write_bytes(pickle.dumps(obj))
    return obj


def pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def hist(x, bins: int, color: str, hover: str, **kw) -> go.Bar:
    """Pre-binned histogram: ships bin counts to the page instead of every raw value."""
    counts, edges = np.histogram(np.asarray(x)[np.isfinite(x)], bins=bins)
    mids = (edges[:-1] + edges[1:]) / 2
    return go.Bar(x=mids, y=counts, width=np.diff(edges), marker_color=color, marker_line_width=0,
                  hovertemplate=hover, **kw)


# ====================================================================== 1 · preprocessing

def report_preprocessing(pp: preprocess.Preprocessed, N: features.Normalised) -> Page:
    c, f, steps = pp.comments, pp.facts, pp.steps
    raw_rows = steps[0].rows_in
    pg = Page("01_preprocessing.html", "Preprocessing report",
              "From the raw Telegram export to a clean comment table and a normalised account × metric matrix. "
              "Every number on this page is computed by <code>engine/preprocess.py</code> and "
              "<code>engine/features.py</code> during the same run.")
    pg.kpis([("raw rows", f"{raw_rows:,}"), ("clean comments", f"{len(c):,}"),
             ("accounts (after identity merge)", f"{c.account.nunique():,}"),
             (f"accounts with {cfg.MIN_COMMENTS}+ comments", f"{c[c.in_model].account.nunique():,}"),
             ("comments covered by modelled accounts", pct(c.in_model.mean())),
             ("metrics per account", f"{N.Z.shape[1]}")])

    pg.h2("Pipeline at a glance")
    pg.p("Each step either <b>filters</b> rows, <b>transforms</b> columns or <b>flags</b> rows without removing "
         "them. Flags are preferred whenever a row is still useful for some signal: a sticker reply has no text "
         "but its timing is real.")
    filt = [s for s in steps if s.kind == "filter"]
    fig = go.Figure(go.Funnel(
        y=[s.name for s in filt], x=[s.rows_out for s in filt], textinfo="value+percent initial",
        marker=dict(color=COLORS[0]),
        hovertext=[f"{s.rows_in - s.rows_out:,} removed<br>{s.why}" for s in filt],
        hovertemplate="<b>%{y}</b><br>%{x:,} rows left<br>%{hovertext}<extra></extra>"))
    pg.fig(fig, "Rows left after each filter",
           "Each bar is what survives the step. The big drop is by design: posts and media are not comments. "
           "The last bar is the subset that enters the statistical models; the rest still feeds pair and text "
           "signals as partners of active accounts.", height=330)
    tbl = pd.DataFrame([{"step": s.name, "kind": s.kind, "rows in": s.rows_in, "rows out": s.rows_out,
                         "removed": s.rows_in - s.rows_out, "% of raw": (s.rows_in - s.rows_out) / raw_rows * 100,
                         "what happened": s.detail, "why": s.why} for s in steps])
    pg.table(tbl, {"rows in": "{:,}", "rows out": "{:,}", "removed": "{:,}", "% of raw": "{:.2f}"}, index=False)

    pg.h2("Author identity")
    pg.p("The export has no user id: <code>name_orig</code> is the display name, and for some accounts Telegram "
         "prefixes it with the username (handle). A handle is unique and stable; a display name is neither.")
    pg.math(r"\text{account}(r)=\begin{cases}\texttt{@}\,\mathrm{lower}(h(r)) & \text{if a handle }h(r)\text{ "
            r"is found}\\ \texttt{\textasciitilde}\,\mathrm{casefold}(\mathrm{NFKC}(\text{name}(r))) & "
            r"\text{otherwise}\end{cases}")
    q_rows, q_acc = f["id_quality_rows"], f["id_quality_accounts"]
    order = ["strong", "probable", "name"]
    fig = go.Figure([
        go.Bar(name="comments", x=order, y=[q_rows.get(k, 0) for k in order], marker_color=COLORS[0],
               hovertemplate="%{x}: %{y:,} comments<extra></extra>"),
        go.Bar(name="accounts", x=order, y=[q_acc.get(k, 0) for k in order], marker_color=COLORS[1],
               hovertemplate="%{x}: %{y:,} accounts<extra></extra>")])
    fig.update_layout(barmode="group", yaxis_title="count")
    b1 = pg.fig(fig, "How sure is each identity?",
                "<b>strong</b>: the first word is a Telegram username (digit, underscore or camelCase). "
                "<b>probable</b>: a lowercase Latin word before a Cyrillic name. <b>name</b>: only the display name, "
                "so two different people with the same name merge and one person who renames splits. Signals built "
                "on <b>name</b> accounts are weaker evidence.")
    per = f["per_account"].sort_values(ascending=False)
    ccdf = pd.Series(np.arange(1, len(per) + 1) / len(per), index=per.values)
    ccdf = ccdf[~ccdf.index.duplicated(keep="last")]
    fig = go.Figure(go.Scatter(x=ccdf.index, y=ccdf.values, mode="lines", line_color=COLORS[0],
                               hovertemplate="%{y:.1%} of accounts have ≥ %{x} comments<extra></extra>"))
    fig.add_vline(cfg.MIN_COMMENTS, line_dash="dot", line_color=COLORS[3],
                  annotation_text=f"model threshold = {cfg.MIN_COMMENTS}")
    fig.update_layout(xaxis_type="log", yaxis_type="log", xaxis_title="comments per account (k)",
                      yaxis_title="P(comments ≥ k)")
    b2 = pg.fig(fig, "Activity per account (CCDF, log-log)",
                "A near-straight line on log-log axes is a heavy (power-law-like) tail: a few accounts write a lot, "
                "most write once. That is normal for online discussion. The dotted line is where accounts start to "
                "get behavioural metrics.")
    pg.grid([b1, b2])
    pg.p(f"{f['accounts_multi_name']} accounts appear under 2+ raw names after keying on the handle "
         "(display-name changes, emoji edits). Examples:")
    ex = f["merged_examples"].map(lambda v: " | ".join(map(str, v))).rename("raw names").to_frame()
    pg.table(ex)
    pg.h3("Removed: authors that are not one person")
    pg.p("Telegram shows hidden or deleted senders and anonymous admins under a fixed placeholder, and a channel's "
         "discussion chat can post as itself. Each would become one fake account made of many people.")
    pg.table(f["non_individual"].head(15), {"rows": "{:,}"}, index=False)
    pg.note(f"<b>Known limit: name collisions.</b> {pct(f['generic_share'])} of the remaining comments come from "
            "name-only accounts whose name is a single word, e.g. "
            + ", ".join(f"{esc(k)} ({v})" for k, v in f["generic_top"].head(8).items())
            + ". Several different people called 'Сергей' merge into one account here. Such accounts look busier "
              "and more spread out than any one person; treat their activity and breadth metrics with care.")

    pg.h2("Text normalisation")
    pg.p("Comparison key: Unicode NFKC, casefold, remove URLs, @mentions, punctuation and emoji, collapse spaces. "
         "Two comments count as the same text only if their keys match.")
    pg.math(r"\text{key}(s)=\mathrm{squash}\big(\mathrm{strip}_{\text{url, @, punct, emoji}}"
            r"(\mathrm{casefold}(\mathrm{NFKC}(s)))\big)")
    flags = pd.Series(f["text_flags"])
    fig = go.Figure(go.Bar(x=flags.values, y=flags.index, orientation="h", marker_color=COLORS[2],
                           text=[f"{v:,} ({v / len(c):.1%})" for v in flags.values], textposition="auto",
                           hovertemplate="%{y}: %{x:,}<extra></extra>"))
    b1 = pg.fig(fig, "Flags on the clean comments",
                "These rows are kept. Media-only and emoji comments are excluded from text metrics but still count "
                "for timing and co-activity. Links are rare: any metric built on links will have little variance.",
                height=260)
    tl = c.loc[c.text_len > 0, "text_len"]
    fig = go.Figure(hist(np.log10(tl), 60, COLORS[0], "10^%{x:.2f} chars: %{y:,} comments<extra></extra>"))
    fig.update_layout(xaxis_title="log10(characters after normalisation)", yaxis_title="comments")
    b2 = pg.fig(fig, "Normalised text length",
                "One broad hump of short replies. Very long texts (10^3+) are pasted articles or manifestos. "
                f"Duplicate-text metrics only use texts of {cfg.DUP_MIN_CHARS}+ characters so that '+' or 'да' "
                "never count as copy-paste.", height=260)
    pg.grid([b1, b2])
    pg.h3("Removed: Telegram service bots")
    pg.p(f"Kept for the signals: <b>{f['copy_paste_texts']:,}</b> texts of {cfg.DUP_MIN_CHARS}+ characters posted "
         f"by 2+ accounts and <b>{f['self_repeat_texts']:,}</b> texts one account repeats 3+ times.")
    sv = f["service_accounts"].copy()
    sv["example"] = sv.example.str.slice(0, 120) + "…"
    pg.table(sv, {"rows": "{:,}"})
    pg.note("A first rule removed any long text repeated 10+ times by at most 3 accounts. It also caught "
            "<i>ledi_morja</i> posting the same insult 11 times, which is behaviour we want to measure, not drop. "
            "The final rule uses Telegram's own constraint that bot usernames end in <code>bot</code>.")

    pg.h2("Threads and timing")
    pt = f["parent_types"].rename(index={"SOCIAL_MEDIA_COMMENT": "reply to a comment",
                                         "SOCIAL_MEDIA_POST": "reply to the post", "missing": "no parent / not in export"})
    fig = go.Figure(go.Pie(labels=pt.index, values=pt.values, hole=0.5, marker_colors=COLORS[:3],
                           hovertemplate="%{label}: %{value:,} (%{percent})<extra></extra>"))
    b1 = pg.fig(fig, "What does a comment answer?",
                "More comments answer other comments than the post: discussions are threaded, so we walk the chain "
                "to the root post. Orphans keep timing metrics but have no 'same post' signal.", height=300)
    lat = c.latency_post_s.dropna()
    fig = go.Figure(hist(np.log10(lat + 1), 80, COLORS[1], "10^%{x:.2f} s after the post: %{y:,}<extra></extra>"))
    for s, lbl in [(60, "1 min"), (3600, "1 h"), (86400, "1 day")]:
        fig.add_vline(np.log10(s), line_dash="dot", line_color="#999", annotation_text=lbl)
    fig.update_layout(xaxis_title="log10(seconds from root post to comment)", yaxis_title="comments")
    b2 = pg.fig(fig, "Delay from post to comment",
                "The bulk of comments arrive between ten minutes and a few hours after the post. Watch the far "
                "left: comments within seconds of a post are humanly hard and become the fast-reply metric.",
                height=300)
    pg.grid([b1, b2])
    pg.p(f"Root post found for {pct(f['root_found'])} of comments. Thread depth (1 = direct reply to the post): "
         + ", ".join(f"{int(k)}: {v:,}" for k, v in f["depth"].head(8).items()) + ".")

    pg.h2("Feature normalisation")
    pg.p("Metrics live on very different scales (counts, seconds, shares, bits) and many are heavy-tailed or "
         "mostly zero. Each metric gets the same treatment:")
    pg.math(r"t_j=\begin{cases}\log(1+x_j)&\text{counts, durations}\\x_j&\text{shares, entropies}\end{cases}"
            r"\qquad z_j=\mathrm{clip}_{[-5,5]}\!\left(\frac{t_j-\operatorname{med}(t_j)}{s_j}\right),\quad "
            r"s_j=1.4826\,\operatorname{MAD}(t_j)")
    pg.p("The median and MAD (median absolute deviation, scaled to equal the standard deviation for normal data) "
         "ignore the extreme tail, so one account with 1,500 comments cannot squash everyone else to zero. When "
         "more than half the values are identical (MAD = 0) the scale falls back to IQR/1.349, then to the "
         "standard deviation. Missing values (e.g. no text comments for a text metric) are imputed with the median, "
         "which places them at z = 0, the uninformative centre.")
    lg = N.log.copy()
    pg.table(lg, {"missing_%": "{:.2f}", "skew_raw": "{:.2f}", "skew_after": "{:.2f}", "clipped_%": "{:.2f}",
                  "zero_share_%": "{:.1f}", "bimodality": "{:.2f}"})
    pg.note("<b>Zero-inflation.</b> Several metrics are 0 for 60-97% of accounts (both_sides, cross_dup_share, "
            "url_share…). Their z-scores form a spike plus a sparse tail. Gaussian models dislike this; the models "
            "report shows the consequence and the counter-measure.")
    cols = list(N.Z.columns)
    rows = int(np.ceil(len(cols) / 4))
    fig = make_subplots(rows=rows, cols=4, subplot_titles=cols, vertical_spacing=0.06)
    for i, col in enumerate(cols):
        fig.add_trace(hist(N.Z[col], 40, GROUP_COLOR[REGISTRY[col].group],
                           f"{col}<br>z=%{{x:.2f}}: %{{y}} accounts<extra></extra>", showlegend=False),
                      row=i // 4 + 1, col=i % 4 + 1)
    fig.update_annotations(font_size=11)
    pg.fig(fig, "Every metric after normalisation (z-scores, colour = metric group)",
           "A good metric has spread: a single bar at 0 means almost every account has the same value and the "
           "metric cannot tell anyone apart. Bars piled at ±5 are clipped extremes. Two separate humps hint at two "
           "populations.", height=170 * rows)

    pg.h2("Summary in plain words")
    pg.plain(f"We started with {raw_rows:,} rows. We kept the {steps[1].rows_out:,} reader comments, removed the "
             f"comments written by the channels' own bots, and merged author names into "
             f"{c.account.nunique():,} accounts using the Telegram username where one is visible. For "
             f"{pct((c.id_quality == 'name').mean())} of comments we only have a display name, so identity is the weakest part of this data. Each of the "
             f"{c[c.in_model].account.nunique():,} accounts with at least {cfg.MIN_COMMENTS} comments is now described "
             f"by {N.Z.shape[1]} numbers on a common scale, ready for the models.")
    return pg


# ====================================================================== 2 · models

def account_info(c: pd.DataFrame, idx: pd.Index) -> pd.DataFrame:
    g = c[c.in_model].groupby("account")
    return pd.DataFrame({
        "name": g.name_clean.agg(lambda s: s.mode().iloc[0]),
        "n": g.size(),
        "main_channel": g.channel.agg(lambda s: s.mode().iloc[0]),
        "ua": g.side.apply(lambda s: (s == "ua").mean()),
    }).reindex(idx)


def hover(info: pd.DataFrame) -> list[str]:
    return [f"<b>{esc(r.name)[:40]}</b><br>{r.n} comments · {r.main_channel}<br>UA share {r.ua:.0%}"
            for r in info.itertuples()]


def scatter_by(scores, labels, info, names: dict | None = None) -> go.Figure:
    fig = go.Figure()
    txt = np.array(hover(info))
    for i, k in enumerate(sorted(np.unique(labels), key=lambda v: (v == -1, v))):
        m = labels == k
        name = (names or {}).get(k, f"{k}")
        fig.add_trace(go.Scattergl(x=scores[m, 0], y=scores[m, 1], mode="markers", name=f"{name} ({m.sum()})",
                                   marker=dict(size=4, opacity=0.6,
                                               color="#c4c8cf" if k == -1 else COLORS[i % len(COLORS)]),
                                   text=txt[m], hovertemplate="%{text}<extra>" + str(name) + "</extra>"))
    fig.update_layout(xaxis_title="PC1", yaxis_title="PC2")
    return fig


def report_models(N, lat, mix, cl, an, sep, info) -> Page:
    Z = N.Z
    n, p = Z.shape
    pg = Page("02_models.html", "Models & theory",
              "Five mathematical views of the same account × metric matrix. For each: what it does, the formula, "
              "which of our four objectives it answers, how to read the chart, and what it found here.")
    pg.h2("The object every model sees")
    pg.math(r"Z\in\mathbb{R}^{n\times p},\quad n=" + f"{n:,}" + r"\text{ accounts},\; p=" + str(p)
            + r"\text{ metrics},\quad z_i\in\mathbb{R}^p\text{ is one account's behavioural profile.}")
    pg.p("Nothing below uses labels: there is no 'bot' column in the data. The models describe structure; "
         "deciding whether a structure is a swarm needs the pair and content evidence on top.")
    pg.table(pd.DataFrame({
        "objective": ["Clustering: do the data separate into 2+ populations?",
                      "Distribution estimation: learn the distribution without knowing how many parts",
                      "Anomaly detection: learn the dominant population, flag what deviates",
                      "Distribution separation: are two populations statistically distinguishable?"],
        "main tools": ["K-means + silhouette, HDBSCAN, GMM with BIC",
                       "KDE, Dirichlet-process Gaussian mixture",
                       "GMM likelihood −log p(x), Isolation Forest",
                       "ΔBIC (1 vs 2 components), Bhattacharyya distance, Bayes-error bound, KS per metric"]}),
        index=False)

    # ---------------- latent
    pg.h2("1 · Latent variable modelling (PCA, factor analysis)")
    pg.p("<b>What it does.</b> Assumes the p metrics are driven by a few hidden behaviour styles and recovers them. "
         "PCA finds orthogonal directions of maximum variance; factor analysis models each metric as a mix of a few "
         "shared factors plus its own noise.")
    pg.math(r"\text{PCA: }\;\hat\Sigma=\tfrac1n Z^\top Z=V\Lambda V^\top,\qquad "
            r"v_1=\arg\max_{\|v\|=1}v^\top\hat\Sigma v,\qquad \text{share}_k=\lambda_k/\textstyle\sum_j\lambda_j")
    pg.math(r"\text{FA: }\;z=\Lambda f+\varepsilon,\quad f\sim\mathcal N(0,I_q),\;\varepsilon\sim\mathcal N(0,\Psi),"
            r"\;\Psi\text{ diagonal}\;\Rightarrow\;\operatorname{Cov}(z)=\Lambda\Lambda^\top+\Psi")
    ev = lat.pca.explained_variance_ratio_
    fig = go.Figure([go.Bar(x=[f"PC{i + 1}" for i in range(len(ev))], y=ev, marker_color=COLORS[0], name="per PC",
                            hovertemplate="%{x}: %{y:.1%}<extra></extra>"),
                     go.Scatter(x=[f"PC{i + 1}" for i in range(len(ev))], y=np.cumsum(ev), name="cumulative",
                                line_color=COLORS[1], hovertemplate="up to %{x}: %{y:.1%}<extra></extra>")])
    fig.add_hline(0.8, line_dash="dot", line_color="#999")
    fig.update_layout(yaxis_tickformat=".0%")
    b1 = pg.fig(fig, "Scree plot: variance explained",
                f"A sharp elbow means a few styles explain behaviour. Here {lat.n_for_80} components reach 80%: "
                "behaviour is genuinely multi-dimensional, no single 'bot score' axis exists.")
    L = lat.loadings.iloc[:, :6]
    fig = go.Figure(go.Heatmap(z=L.values, x=L.columns, y=L.index, colorscale="RdBu", zmid=0, zmin=-1, zmax=1,
                               hovertemplate="%{y} on %{x}: r = %{z:.2f}<extra></extra>"))
    fig.update_layout(yaxis_autorange="reversed")
    b2 = pg.fig(fig, "PCA loadings (correlation of metric with component)",
                "Read a column: the strongly red/blue metrics name the component. Metrics that load together "
                "measure the same latent behaviour, so they are partly redundant.", height=520)
    pg.grid([b1, b2])
    F = lat.fa_loadings
    fig = go.Figure(go.Heatmap(z=F.values, x=F.columns, y=F.index, colorscale="RdBu", zmid=0, zmin=-1, zmax=1,
                               hovertemplate="%{y} on %{x}: %{z:.2f}<extra></extra>"))
    fig.update_layout(yaxis_autorange="reversed")
    top = {f: ", ".join(F[f].abs().sort_values(ascending=False).index[:3]) for f in F}
    pg.fig(fig, "Factor analysis loadings (varimax rotated)",
           "Varimax rotation pushes each metric to load on one factor, so factors are easier to name than PCs. "
           "Use them as candidate behaviour styles: " + "; ".join(f"<b>{k}</b> ≈ {v}" for k, v in top.items()),
           height=520)

    # ---------------- mixtures + distribution estimation
    pg.h2("2 · Mixture models and distribution estimation")
    pg.p("<b>What it does.</b> Models the whole distribution as a weighted sum of Gaussian components, each a "
         "candidate population. Fitted by EM; the number of components is chosen by BIC, or learnt directly with a "
         "Dirichlet-process prior that switches unneeded components off.")
    pg.math(r"p(z)=\sum_{k=1}^{K}\pi_k\,\mathcal N(z\mid\mu_k,\Sigma_k),\qquad "
            r"\gamma_{ik}=\frac{\pi_k\mathcal N(z_i\mid\mu_k,\Sigma_k)}{\sum_l\pi_l\mathcal N(z_i\mid\mu_l,\Sigma_l)}"
            r"\;\;(\text{E-step}),\qquad \mu_k=\frac{\sum_i\gamma_{ik}z_i}{\sum_i\gamma_{ik}}\;\;(\text{M-step})")
    pg.math(r"\mathrm{BIC}(K)=-2\log\hat L_K+m_K\log n,\qquad m_K=K-1+Kp+K\tfrac{p(p+1)}{2}")
    pg.math(r"\text{DP prior (stick breaking): }\;\beta_k\sim\mathrm{Beta}(1,\alpha),\;"
            r"\pi_k=\beta_k\prod_{l<k}(1-\beta_l),\quad\alpha\text{ small}\Rightarrow\text{few components get weight}")
    fig = go.Figure([go.Scatter(x=list(mix.bic.index), y=mix.bic.values, name="BIC", line_color=COLORS[0],
                                hovertemplate="K=%{x}: BIC %{y:,.0f}<extra></extra>"),
                     go.Scatter(x=list(mix.aic.index), y=mix.aic.values, name="AIC", line_color=COLORS[5],
                                line_dash="dot", hovertemplate="K=%{x}: AIC %{y:,.0f}<extra></extra>")])
    fig.add_vline(mix.k_best, line_dash="dot", line_color=COLORS[3], annotation_text=f"K* = {mix.k_best}")
    fig.update_layout(xaxis_title="number of components K", yaxis_title="criterion (lower is better)")
    b1 = pg.fig(fig, "How many populations? BIC vs K",
                "The minimum is the best trade-off between fit and complexity. A big drop from K=1 to K=2 already "
                "says 'more than one population'. A curve that keeps falling means the model is fitting fine "
                "detail (or artefacts like exact zeros), not new populations.")
    S = mix.reg_sensitivity
    fig = go.Figure(go.Heatmap(z=np.log10(S.values + 1), x=[str(k) for k in S.columns],
                               y=[f"floor {r}" for r in S.index], colorscale="Blues_r",
                               customdata=S.values, hovertemplate="%{y}, K=%{x}: ΔBIC %{customdata:,.0f}<extra></extra>"))
    for r in S.index:
        k = S.loc[r].idxmin()
        fig.add_annotation(x=str(k), y=f"floor {r}", text="★", showarrow=False, font=dict(color="white", size=16))
    fig.update_layout(xaxis_title="K", height=240)
    b2 = pg.fig(fig, "Sensitivity: best K under three covariance floors",
                "★ marks the best K for each floor (darker = closer to the best). If ★ moves a lot, the exact number "
                "of populations is not identifiable from these metrics; only the coarse split is. That is the "
                "honest reading here, caused by zero-inflated metrics.", height=240)
    pg.grid([b1, b2])
    fig = go.Figure(go.Bar(x=[f"c{i + 1}" for i in range(len(mix.dp_weights))], y=mix.dp_weights,
                           marker_color=[COLORS[0] if w > 0.01 else "#c4c8cf" for w in mix.dp_weights],
                           hovertemplate="%{x}: weight %{y:.3f}<extra></extra>"))
    fig.add_hline(0.01, line_dash="dot", line_color="#999", annotation_text="1% cut")
    b1 = pg.fig(fig, "Dirichlet-process mixture: component weights",
                f"The model was offered {len(mix.dp_weights)} components and kept {mix.dp_effective} above 1%. "
                "This is distribution estimation without fixing the number in advance. Compare with K* from BIC: "
                "agreement means the count is robust.")
    names = {k: f"component {k}" for k in np.unique(mix.labels)}
    b2 = pg.fig(scatter_by(lat.scores, mix.labels, info, names), f"Accounts in PC space, coloured by GMM component (K={mix.k_best})",
                "Hover a point for the account. Components that sit apart in this 2D view are well separated; "
                "overlapping colours can still be separate in the other dimensions. Small, tight components are the "
                "interesting ones for swarm work.")
    pg.grid([b1, b2])
    sizes = pd.Series(mix.labels).value_counts().sort_index()
    prof = Z.groupby(mix.labels).mean().T
    prof.columns = [f"c{k} (n={sizes[k]})" for k in prof.columns]
    fig = go.Figure(go.Heatmap(z=prof.values, x=prof.columns, y=prof.index, colorscale="RdBu_r", zmid=0, zmin=-3, zmax=3,
                               hovertemplate="%{x}<br>%{y}: mean z = %{z:.2f}<extra></extra>"))
    fig.update_layout(yaxis_autorange="reversed")
    pg.fig(fig, "Component profiles: mean z-score per metric",
           "Each column is a population. Red = above the typical account, blue = below. A component with red on "
           "crowding, repeat_partners, fast_reply and cross_dup together is the swarm-like profile; red on "
           "n_comments alone is just a power user.", height=560)

    # ---------------- clustering
    pg.h2("3 · Clustering (K-means, HDBSCAN)")
    pg.p("<b>What it does.</b> Partitions accounts into groups of similar profiles. K-means assumes round groups of "
         "similar size; HDBSCAN finds dense regions of any shape and leaves sparse points as noise, which suits "
         "small coordinated groups inside a large organic crowd.")
    pg.math(r"\text{K-means: }\min_{C_1..C_K}\sum_k\sum_{i\in C_k}\|z_i-\bar z_k\|^2,\qquad "
            r"s(i)=\frac{b(i)-a(i)}{\max\{a(i),b(i)\}}\in[-1,1]")
    pg.math(r"\text{HDBSCAN: }d_{\text{mreach}}(a,b)=\max\{\mathrm{core}_m(a),\mathrm{core}_m(b),\|a-b\|\},"
            r"\;\text{clusters = most persistent branches of its single-linkage tree}")
    pg.p("<i>a(i)</i> is the mean distance to the own cluster, <i>b(i)</i> to the nearest other cluster. Silhouette "
         "≈ 0.25 means weak structure, ≥ 0.5 strong.")
    fig = go.Figure(go.Scatter(x=list(cl.silhouette.index), y=cl.silhouette.values, mode="lines+markers",
                               line_color=COLORS[0], hovertemplate="K=%{x}: silhouette %{y:.3f}<extra></extra>"))
    fig.update_layout(xaxis_title="K", yaxis_title="mean silhouette")
    b1 = pg.fig(fig, "K-means: silhouette vs K",
                "Peaks mark natural group counts. Values around 0.2-0.3 say the groups overlap: accounts form a "
                "continuum with denser regions, not cleanly separated clouds.")
    hl = cl.hdb_labels
    names = {k: ("noise" if k == -1 else f"cluster {k}") for k in np.unique(hl)}
    b2 = pg.fig(scatter_by(lat.scores, hl, info, names), "HDBSCAN clusters in PC space",
                f"Grey = noise (no dense group). HDBSCAN ran on the first {lat.n_for_80} PCs. Small clusters far "
                "from the main mass are candidates; check their profile below before calling them anything.")
    pg.grid([b1, b2])
    sizes = pd.Series(hl).value_counts().sort_index()
    prof = Z.groupby(hl).mean().T
    prof.columns = [f"{'noise' if k == -1 else 'h' + str(k)} (n={sizes[k]})" for k in prof.columns]
    fig = go.Figure(go.Heatmap(z=prof.values, x=prof.columns, y=prof.index, colorscale="RdBu_r", zmid=0, zmin=-3, zmax=3,
                               hovertemplate="%{x}<br>%{y}: mean z = %{z:.2f}<extra></extra>"))
    fig.update_layout(yaxis_autorange="reversed")
    pg.fig(fig, "HDBSCAN cluster profiles",
           "Same reading as the GMM profiles. If a GMM component and an HDBSCAN cluster share a profile, two "
           "different assumptions found the same group: stronger evidence.", height=560)
    pg.p(f"Agreement between partitions (adjusted Rand index, 0 = chance, 1 = identical): GMM vs K-means "
         f"{adjusted_rand_score(mix.labels, cl.kmeans_labels):.2f}, GMM vs HDBSCAN "
         f"{adjusted_rand_score(mix.labels, hl):.2f}, K-means vs HDBSCAN {adjusted_rand_score(cl.kmeans_labels, hl):.2f}.")

    # ---------------- density
    pg.h2("4 · Density estimation (KDE)")
    pg.p("<b>What it does.</b> Estimates the probability density of each metric without assuming a shape: every "
         "account contributes a small Gaussian bump, and the bumps add up. Multiple modes in a metric are the "
         "simplest evidence of multiple populations.")
    pg.math(r"\hat f_h(x)=\frac1{nh}\sum_{i=1}^n K\!\left(\frac{x-x_i}{h}\right),\quad "
            r"h=0.9\min\!\left(\hat\sigma,\tfrac{\mathrm{IQR}}{1.34}\right)n^{-1/5}\;(\text{Silverman})")
    pg.math(r"\mathrm{BC}=\frac{g^2+1}{\kappa+\frac{3(n-1)^2}{(n-2)(n-3)}}\;>\;\tfrac59\;\Rightarrow\;"
            r"\text{evidence of bi- or multimodality}\quad(g=\text{skew},\;\kappa=\text{excess kurtosis})")
    cols = list(N.T.columns)
    rows = int(np.ceil(len(cols) / 4))
    titles = [f"{col} · BC {N.log.loc[col, 'bimodality']:.2f}" for col in cols]
    fig = make_subplots(rows=rows, cols=4, subplot_titles=titles, vertical_spacing=0.06)
    for i, col in enumerate(cols):
        xs, ys, h = models.kde_1d(N.T[col].to_numpy())
        bc = N.log.loc[col, "bimodality"]
        fig.add_trace(go.Scatter(x=xs, y=ys, mode="lines", fill="tozeroy", showlegend=False,
                                 line_color=COLORS[3] if bc > 5 / 9 else COLORS[0],
                                 hovertemplate=f"{col} = %{{x:.2f}}<br>density %{{y:.3f}}<extra>h={h:.3g}</extra>"),
                      row=i // 4 + 1, col=i % 4 + 1)
    fig.update_annotations(font_size=10)
    pg.fig(fig, "Density of each metric (after transform; pink = BC above 5/9)",
           "Look for a second hump or a shoulder. Pink panels pass the bimodality-coefficient test, but zero-inflated "
           "metrics pass it trivially (spike at 0 + tail): for those, the tail is the population of interest.",
           height=170 * rows)
    gx, gy, dens = models.kde_2d(lat.scores[:, :2])
    fig = go.Figure([go.Contour(x=gx, y=gy, z=dens, colorscale="Blues", showscale=False, contours_coloring="fill",
                                hovertemplate="PC1 %{x:.1f}, PC2 %{y:.1f}<br>density %{z:.4f}<extra></extra>"),
                     go.Scattergl(x=lat.scores[:, 0], y=lat.scores[:, 1], mode="markers",
                                  marker=dict(size=2, color="#1d2330", opacity=0.25), text=hover(info),
                                  hovertemplate="%{text}<extra></extra>", showlegend=False)])
    fig.update_layout(xaxis_title="PC1", yaxis_title="PC2")
    pg.fig(fig, "Joint density in the first two principal components",
           "Dark cores are where typical accounts live. Separate small cores are candidate populations; isolated "
           "points in white space are anomalies (next section).", height=460)

    # ---------------- anomaly
    pg.h2("5 · Anomaly detection")
    pg.p("<b>What it does.</b> Learns what the dominant population looks like and scores how unusual each account "
         "is. Two independent definitions are combined: low probability under the fitted mixture, and ease of "
         "isolation by random splits.")
    pg.math(r"a_{\text{GMM}}(z)=-\log\hat p(z),\qquad a_{\text{IF}}(z)=2^{-\mathbb E[h(z)]/c(n)},\;"
            r"c(n)=2H_{n-1}-\tfrac{2(n-1)}{n}")
    pg.math(r"a(z)=\tfrac12\big(\mathrm{rank}(a_{\text{GMM}})+\mathrm{rank}(a_{\text{IF}})\big)/n,\qquad "
            r"\text{flag if }a(z)\text{ is in the top }2\%")
    pg.p("<i>h(z)</i> is the number of random splits needed to isolate the account: outliers are cut off early. "
         "Rank-averaging puts the two scores on one scale without trusting either's units.")
    fl = an.flag
    txt = np.array(hover(info))
    fig = go.Figure([go.Scattergl(x=an.gmm_score[~fl], y=an.iforest_score[~fl], mode="markers", name="typical",
                                  marker=dict(size=4, color="#c4c8cf"), text=txt[~fl],
                                  hovertemplate="%{text}<extra></extra>"),
                     go.Scattergl(x=an.gmm_score[fl], y=an.iforest_score[fl], mode="markers",
                                  name=f"flagged ({fl.sum()})", marker=dict(size=6, color=COLORS[3]), text=txt[fl],
                                  hovertemplate="%{text}<extra>flagged</extra>")])
    fig.update_layout(xaxis_title="−log p(z) under the GMM", yaxis_title="Isolation Forest score")
    pg.fig(fig, "Two anomaly scores against each other",
           f"Points top-right are unusual for both methods (Spearman ρ = {an.spearman:.2f}; "
           f"{an.top_overlap:.0%} of each method's top 2% is shared). An anomaly is not a bot: power users, "
           "night-shift readers and cross-side debaters are also rare. Use the 'why' column below.", height=420)
    top_idx = np.argsort(-an.combined)[:20]
    rows = []
    for i in top_idx:
        z = Z.iloc[i]
        why = z.abs().sort_values(ascending=False).index[:3]
        rows.append({"account": info.name.iloc[i][:40], "comments": info.n.iloc[i],
                     "channel": info.main_channel.iloc[i], "score": an.combined[i],
                     "why (top |z|)": ", ".join(f"{m} {z[m]:+.1f}" for m in why)})
    pg.table(pd.DataFrame(rows), {"score": "{:.3f}"}, index=False)

    # ---------------- separation
    pg.h2("6 · Distribution separation")
    pg.p("<b>What it does.</b> Asks a yes/no question with a number attached: is a two-population model "
         "significantly better than one, and if so, how distinguishable are the two populations?")
    pg.math(r"\Delta\mathrm{BIC}=\mathrm{BIC}(1)-\mathrm{BIC}(2),\qquad \Delta\mathrm{BIC}>10\;\Rightarrow\;"
            r"\text{very strong evidence for two populations (Kass \& Raftery)}")
    pg.math(r"D_B=\tfrac18(\mu_1-\mu_2)^\top\Sigma^{-1}(\mu_1-\mu_2)+\tfrac12\ln\frac{\det\Sigma}"
            r"{\sqrt{\det\Sigma_1\det\Sigma_2}},\qquad\Sigma=\tfrac12(\Sigma_1+\Sigma_2)")
    pg.p("<b>Proof of the error bound.</b> The best possible classifier between the two populations errs with "
         "probability")
    pg.math(r"P_e=\int\min\{\pi_1p_1(z),\pi_2p_2(z)\}\,dz\;\le\;\int\sqrt{\pi_1p_1\,\pi_2p_2}\,dz"
            r"=\sqrt{\pi_1\pi_2}\,\underbrace{\int\sqrt{p_1p_2}\,dz}_{\text{Bhattacharyya coeff.}}"
            r"=\sqrt{\pi_1\pi_2}\,e^{-D_B}")
    pg.p("using min(a, b) ≤ √(ab) for a, b ≥ 0; for Gaussians the integral has the closed form e<sup>−D_B</sup>. "
         "So a large D<sub>B</sub> proves that no method can confuse the two populations often.")
    pg.kpis([("ΔBIC (1 → 2 components)", f"{sep.delta_bic:,.0f}"), ("Bhattacharyya distance D_B", f"{sep.bhattacharyya:.2f}"),
             ("Bayes error upper bound", f"{sep.bayes_error_bound:.2e}"),
             ("population sizes", f"{sep.weights[0]:.0%} / {sep.weights[1]:.0%}")])
    pm = sep.per_metric.sort_values("ks", ascending=True)
    fig = go.Figure(go.Bar(x=pm.ks, y=pm.index, orientation="h",
                           marker_color=[GROUP_COLOR[REGISTRY[m].group] for m in pm.index],
                           customdata=np.c_[pm.cohen_d, pm.auc],
                           hovertemplate="%{y}<br>KS %{x:.2f} · Cohen d %{customdata[0]:+.2f} · AUC "
                                         "%{customdata[1]:.2f}<extra></extra>"))
    fig.update_layout(xaxis_title="KS distance between the two components (0 = same, 1 = disjoint)")
    pg.fig(fig, "Which metrics separate the two populations?",
           "Long bars = the metric alone tells the populations apart. Cohen's d gives the direction (+ = the "
           "smaller population is higher). AUC 0.5 = useless alone, 1 = perfect. Every p-value is tiny with "
           f"n = {n:,}, so read the effect sizes, not the p-values.", height=520)
    pg.note("The two populations are found by the mixture, then compared on the same metrics, so their "
            "separability is partly built in. ΔBIC is the honest test (one vs two); D<sub>B</sub> and KS describe "
            "<i>how</i> they differ. A validation set of known swarm accounts would turn this into a real test.")

    pg.h2("Summary in plain words")
    smaller = sep.per_metric.sort_values("ks", ascending=False).index[:3]
    pg.plain(f"Accounts do not form one homogeneous crowd: a two-group model beats a one-group model by a huge "
             f"margin (ΔBIC {sep.delta_bic:,.0f}), and the two groups are almost never confused (error ≤ "
             f"{sep.bayes_error_bound:.1e}). The split is driven mainly by {', '.join(smaller)}. The exact number "
             f"of finer groups depends on modelling choices (BIC best K = {mix.k_best}; the Dirichlet process kept "
             f"{mix.dp_effective} of {len(mix.dp_weights)} offered"
             f"{', i.e. it hit the cap and wants more' if mix.dp_effective == len(mix.dp_weights) else ''}), "
             f"so we treat fine groups as leads, not facts. {int(an.flag.sum())} accounts are "
             f"flagged as unusual by both anomaly methods combined; the table says why for each. None of this says "
             f"'bot' yet: it says where to look, and the coordination evidence (who acts with whom) decides.")
    return pg


# ====================================================================== 3 · metrics

def report_metrics(N, mix, an, ev) -> Page:
    Z = N.Z
    pg = Page("03_metrics.html", "Metric evaluation",
              "Every registered metric scored on the same criteria: does it carry information, does the model use it, "
              "does it separate populations, and is it redundant with another metric.")
    pg.h2("How a metric enters the system")
    pg.p("A metric is one decorated function in <code>engine/metrics.py</code>. It receives the context (clean "
         "comments, co-activity pairs, …) and returns one number per account. Normalisation, models, this "
         "evaluation and all charts pick it up automatically on the next run.")
    pg.html("<pre class='card'><code>@metric(\"timing\", transform=\"log1p\",\n"
            "        description=\"Median seconds from the parent message to the reply.\",\n"
            "        hypothesis=\"Scripted accounts answer on a trigger, so the delay is short and steady.\",\n"
            "        look_for=\"A separate low mode: a group that always answers within seconds.\")\n"
            "def median_parent_latency(ctx):\n"
            "    return ctx.c.groupby(\"account\").latency_parent_s.median()</code></pre>")
    pg.p("<code>ctx.c</code> are the comments of modelled accounts, <code>ctx.all</code> all clean comments, "
         "<code>ctx.pairs</code> the co-activity pairs, <code>ctx.text</code> text comments only. New derived tables "
         "are added to <code>Ctx</code> as cached properties.")

    pg.h2("Criteria")
    pg.p(f"Reference partition: the BIC-best Gaussian mixture (K = {mix.k_best}) from the models report; anomalies: "
         f"the {int(an.flag.sum())} flagged accounts.")
    pg.math(r"F_j=\frac{\sum_k n_k(\bar z_{jk}-\bar z_j)^2}{\sum_k\sum_{i\in k}(z_{ij}-\bar z_{jk})^2}"
            r"\qquad I(z_j;c)=\sum_{c}\int p(z,c)\log\frac{p(z,c)}{p(z)p(c)}\,dz")
    pg.math(r"\mathrm{PI}_j=\mathrm{acc}(f,\,Z_{\text{test}})-\mathbb E_\sigma\,\mathrm{acc}(f,\,Z_{\text{test}}"
            r"^{(j\leftarrow\sigma)})\qquad \mathrm{LOO}_j=1-\mathrm{ARI}\big(c,\;c^{(-j)}\big)")
    pg.math(r"\mathrm{KS}_j=\sup_x\big|F_{j}^{\text{anom}}(x)-F_{j}^{\text{rest}}(x)\big|\qquad "
            r"h_j^2=\sum_{k\le m}\ell_{jk}^2\;(\text{communality on the first }m\text{ PCs})")
    pg.table(pd.DataFrame({"criterion": list(CRITERIA), "meaning": list(CRITERIA.values()),
                           "answers": ["distinguishability", "distinguishability", "how much the model uses it",
                                       "how much the model uses it", "does it expose anomalies",
                                       "is it part of the shared structure"]}), index=False)
    pg.p(f"The surrogate is a random forest trained to predict the mixture component from the metrics "
         f"(held-out accuracy {ev.attrs['surrogate_accuracy']:.1%}); permutation importance is measured on held-out "
         "data. <b>overall</b> is the mean percentile rank over fisher, mutual_info, perm_importance, loo_dependence "
         "and anomaly_ks.")
    pg.note("There are no labels, so 'importance' means importance <i>for the structure the unsupervised models "
            "found</i>, not for finding bots. A metric can rank low here and still be decisive for a specific swarm "
            "(e.g. copy-paste for a small group): low variance across all accounts hides small groups.")

    pg.h2("Ranking")
    o = ev.sort_values("overall")
    fig = go.Figure(go.Bar(x=o.overall, y=o.index, orientation="h",
                           marker_color=[GROUP_COLOR[REGISTRY[m].group] for m in o.index],
                           hovertemplate="%{y}: overall %{x:.2f}<extra></extra>"))
    fig.update_layout(xaxis_title="overall (mean percentile rank, 1 = best on every criterion)")
    b1 = pg.fig(fig, "Overall usefulness (colour = metric group)",
                "Top metrics shape the populations the models found. Bottom ones barely matter to the models: "
                "they are either near-constant, redundant, or specific to small groups. Check 'max_corr' before "
                "dropping one.", height=560)
    crit = list(CRITERIA)
    R = ev[crit].rank(pct=True).loc[o.index[::-1]]
    fig = go.Figure(go.Heatmap(z=R.values, x=crit, y=R.index, colorscale="Viridis", zmin=0, zmax=1,
                               customdata=ev[crit].loc[R.index].values,
                               hovertemplate="%{y} · %{x}<br>value %{customdata:.3g}<br>percentile %{z:.0%}<extra></extra>"))
    fig.update_layout(yaxis_autorange="reversed")
    b2 = pg.fig(fig, "Every criterion (percentile rank across metrics)",
                "Bright across the row = strong on every view. A bright anomaly_ks cell with dark elsewhere: the "
                "metric does not shape the big populations but does mark the unusual accounts.", height=560)
    pg.grid([b1, b2])
    tbl = ev.copy()
    tbl.insert(0, "group", [REGISTRY[m].group for m in tbl.index])
    tbl.insert(1, "missing_%", N.log.loc[tbl.index, "missing_%"])
    tbl.insert(2, "bimodality", N.log.loc[tbl.index, "bimodality"])
    pg.table(tbl, {"missing_%": "{:.2f}", "bimodality": "{:.2f}", "fisher": "{:.3f}", "mutual_info": "{:.3f}",
                   "perm_importance": "{:.4f}", "loo_dependence": "{:.3f}", "anomaly_ks": "{:.2f}",
                   "communality": "{:.2f}", "max_corr": "{:.2f}", "overall": "{:.2f}"})

    pg.h2("Redundancy")
    C = Z.corr(method="spearman")
    fig = go.Figure(go.Heatmap(z=C.values, x=C.columns, y=C.index, colorscale="RdBu", zmid=0, zmin=-1, zmax=1,
                               hovertemplate="%{y} vs %{x}: ρ = %{z:.2f}<extra></extra>"))
    fig.update_layout(yaxis_autorange="reversed")
    pg.fig(fig, "Spearman correlation between metrics",
           "|ρ| above ~0.8 means two metrics measure nearly the same thing; keep the more interpretable one. "
           "Blocks of colour show metric families (activity, breadth…).", height=620)

    pg.h2("Metric cards")
    pg.p("One card per metric: its definition, the hypothesis behind it, and its distribution in each mixture "
         "component, with flagged anomalies on top. Hover the badge for what to look for.")
    lab = mix.labels
    blocks = []
    for m in ev.index:
        meta = REGISTRY[m]
        fig = go.Figure()
        for i, k in enumerate(np.unique(lab)):
            fig.add_trace(go.Box(x=N.T[m][lab == k], name=f"c{k}", marker_color=COLORS[i % len(COLORS)],
                                 boxpoints=False, orientation="h", hoverinfo="x+name"))
        fig.add_trace(go.Box(x=N.T[m][an.flag], name="anomalies", marker_color=COLORS[3], boxpoints="all",
                             jitter=0.5, pointpos=0, orientation="h", hoverinfo="x"))
        fig.update_layout(showlegend=False, xaxis_title=f"{m} ({meta.transform})")
        r = ev.loc[m]
        cap = (f"<b>{esc(meta.description)}</b><br>Hypothesis: {esc(meta.hypothesis)}<br>"
               f"fisher {r.fisher:.3f} · MI {r.mutual_info:.3f} · perm {r.perm_importance:.4f} · "
               f"LOO {r.loo_dependence:.2f} · anomaly KS {r.anomaly_ks:.2f} · most similar: {r.most_similar} "
               f"(ρ {r.max_corr:.2f})")
        blocks.append(pg.fig(fig, f"{m}  ·  {meta.group}", esc(meta.look_for), cap, height=300))
    pg.grid(blocks)

    pg.h2("Summary in plain words")
    best = ", ".join(ev.index[:4])
    worst = ", ".join(ev.index[-3:])
    pg.plain(f"The metrics that most shape the groups the models found are {best}. The least useful for the big "
             f"picture are {worst}: mostly because almost every account has the same value, so they can only "
             "matter for a few accounts. To add a metric, write one function; the next run scores it here on the "
             "same criteria, so we can see at once whether it adds something new or repeats an existing one.")
    return pg


# ====================================================================== main

def main() -> None:
    fresh = "--fresh" in sys.argv
    cfg.CACHE.mkdir(exist_ok=True)
    cfg.RESULTS.mkdir(exist_ok=True)
    print("1/5 preprocessing")
    pp = cached("preprocessed", preprocess.run, fresh)
    print("2/5 metrics")
    raw = metrics.compute(metrics.Ctx(pp.comments))  # always recomputed: new metrics show up at once
    N = features.normalise(raw)
    print("3/5 models")
    lat = models.latent(N.Z)
    mix = models.mixtures(N.Z)
    cl = models.clustering(N.Z, lat.scores, lat.n_for_80)
    an = models.anomaly(N.Z, mix.logp)
    sep = models.separation(N.Z, mix)
    print("4/5 metric evaluation")
    ev = evaluate.score(N.Z, mix, lat, an.flag)
    print("5/5 reports")
    info = account_info(pp.comments, N.Z.index)
    for page in (report_preprocessing(pp, N), report_models(N, lat, mix, cl, an, sep, info),
                 report_metrics(N, mix, an, ev)):
        print("  ", page.save(cfg.RESULTS))
    out = N.raw.join(info).assign(gmm=mix.labels, hdbscan=cl.hdb_labels, anomaly=an.combined, flagged=an.flag)
    out.to_csv(cfg.RESULTS / "accounts.csv.gz")
    print("  ", cfg.RESULTS / "accounts.csv.gz")


if __name__ == "__main__":
    main()
