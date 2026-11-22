"""Profile the 14-week export (project 105) and surface coordinated account groups.

Run (repo root):
  uvx uv@0.11.3 run --locked --package swarm-detector --group analysis python swarm-detector/analysis/weeks14.py

Input : swarm-detector/data/weeks14/*.json   (one file per week, git-ignored)
Output: results/weeks14_report.html and results/weeks14_accounts.csv.gz (git-ignored: real names).

Behaviour only: a group is "acting together", not "bots" or "operated by X".
Grouping rule: heavy accounts in one channel whose active window starts AND ends together.
"""
import argparse
import base64
import glob
import html
import io
import json
from collections import Counter
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.ticker
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
BLUE, RED, GREY = "#2f5f9e", "#c0392b", "#9aa5b1"
plt.rcParams.update({"figure.dpi": 130, "axes.spines.top": False, "axes.spines.right": False,
                     "font.size": 9, "axes.titleweight": "bold", "axes.titlesize": 10})


def load(data_dir: Path, fresh: bool) -> tuple[pd.DataFrame, pd.DataFrame]:
    cache = HERE / "cache" / "weeks14.pkl"
    if cache.exists() and not fresh:
        return pd.read_pickle(cache)
    rows, weeks = [], []
    for f in sorted(glob.glob(str(data_dir / "*.json"))):
        j = json.load(open(f))
        n_p = n_c = 0
        for ch in j["channels"]:
            name = ch["channel"].replace("Telegram: View ", "")
            n_p += len(ch["posts"])
            for kind, items in (("comment", ch["comments"]), ("post", ch["posts"])):
                if kind == "post":
                    continue
                for x in items:
                    rows.append((x["id"], name, x["published_at"], x["author"], x["text"],
                                 x["language"], x["likes"], x["views"]))
            n_c += len(ch["comments"])
        weeks.append((j["week_start"], j["week_end"], len(j["channels"]), n_p, n_c))
    df = pd.DataFrame(rows, columns=["id", "channel", "t", "author", "text", "lang", "likes", "views"])
    df["t"] = pd.to_datetime(df.t, utc=True)
    wk = pd.DataFrame(weeks, columns=["start", "end", "channels", "posts", "comments"]).sort_values("start")
    cache.parent.mkdir(exist_ok=True)
    pd.to_pickle((df, wk), cache)
    return df, wk


def find_groups(df: pd.DataFrame, min_comments: int, tol_min: int) -> pd.DataFrame:
    """Per channel: heavy accounts linked when their 2%..98% activity window starts and ends together."""
    d = df[df.author != "Unknown author"]
    out = []
    for ch, g in d.groupby("channel"):
        cnt = g.author.value_counts()
        heavy = cnt[cnt >= min_comments].index
        if len(heavy) < 3:
            continue
        s = g[g.author.isin(heavy)].groupby("author").t
        acc = pd.DataFrame({"n": s.size(), "start": s.quantile(0.02, interpolation="nearest"),
                            "end": s.quantile(0.98, interpolation="nearest")})
        tol = pd.Timedelta(minutes=tol_min)
        parent = {a: a for a in acc.index}

        def find(a):
            while parent[a] != a:
                parent[a] = parent[parent[a]]
                a = parent[a]
            return a

        idx = list(acc.index)
        for i, a in enumerate(idx):
            for b in idx[i + 1:]:
                if abs(acc.start[a] - acc.start[b]) <= tol and abs(acc.end[a] - acc.end[b]) <= tol:
                    parent[find(a)] = find(b)
        acc["root"] = [find(a) for a in acc.index]
        sizes = acc.root.value_counts()
        acc = acc[acc.root.map(sizes) >= 3].copy()
        acc["channel"] = ch
        out.append(acc.reset_index().rename(columns={"index": "author"}))
    if not out:
        return pd.DataFrame()
    r = pd.concat(out, ignore_index=True)
    r["group"] = (r.channel + "|" + r.root.astype(str)).astype("category").cat.codes
    return r.drop(columns="root")


def summarise_groups(df: pd.DataFrame, acc: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for gid, a in acc.groupby("group"):
        ch = a.channel.iloc[0]
        s = df[(df.channel == ch) & df.author.isin(a.author)]
        ch_total = (df.channel == ch).sum()
        daily = s.groupby([s.author, s.t.dt.date]).size().unstack(fill_value=0)
        core = daily.iloc[:, 1:-1] if daily.shape[1] > 3 else daily
        per_acct_cv = (core.std(axis=1) / core.mean(axis=1).replace(0, np.nan)).median()
        lines = Counter(l for t in s.text for l in str(t).split("\n") if l.strip())
        tot = sum(lines.values())
        reuse = sum(c for c in lines.values() if c >= 30) / tot if tot else 0
        rows.append(dict(
            group=gid, channel=ch, accounts=len(a), comments=len(s), share_of_channel=len(s) / ch_total,
            start=a.start.min(), end=a.end.max(), start_spread_min=(a.start.max() - a.start.min()).total_seconds() / 60,
            end_spread_min=(a.end.max() - a.end.min()).total_seconds() / 60,
            unique_text=s.text.nunique() / len(s), daily_count_cv=per_acct_cv, line_reuse=reuse,
            lines_per_comment=tot / len(s)))
    return pd.DataFrame(rows).sort_values("comments", ascending=False)


def img(fig) -> str:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight")
    plt.close(fig)
    return f'<img src="data:image/png;base64,{base64.b64encode(buf.getvalue()).decode()}">'


def esc(x) -> str:
    return html.escape(str(x))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", type=Path, default=HERE.parent / "data" / "weeks14")
    ap.add_argument("--fresh", action="store_true", help="re-read the JSON instead of the cache")
    ap.add_argument("--min-comments", type=int, default=300)
    ap.add_argument("--tol-min", type=int, default=10, help="minutes: windows must start/end this close")
    a = ap.parse_args()

    df, wk = load(a.data, a.fresh)
    acc = find_groups(df, a.min_comments, a.tol_min)
    gs = summarise_groups(df, acc)
    out = HERE / "results"
    out.mkdir(exist_ok=True)
    acc.merge(gs[["group", "channel"]].drop_duplicates("group"), on=["group", "channel"]).to_csv(
        out / "weeks14_accounts.csv.gz", index=False)

    # ---- charts
    figs = {}
    fig, ax = plt.subplots(figsize=(10, 3))
    ax.bar(wk.start, wk.comments, color=BLUE, label="comments")
    ax.set_title("Comments per week (week starting)")
    ax.set_ylabel("comments")
    ax.tick_params(axis="x", rotation=45)
    figs["weeks"] = img(fig)

    ch = df.channel.value_counts().head(12)[::-1]
    fig, ax = plt.subplots(figsize=(10, 3.4))
    ax.barh(ch.index, ch.values, color=BLUE)
    ax.set_title("Comments per channel (top 12)")
    figs["chan"] = img(fig)

    in_group = df.set_index(["channel", "author"]).index.isin(acc.set_index(["channel", "author"]).index)
    daily = pd.DataFrame({"swarm candidates": df[in_group].groupby(df.t.dt.date).size(),
                          "everyone else": df[~in_group].groupby(df.t.dt.date).size()}).fillna(0)
    fig, ax = plt.subplots(figsize=(10, 3.4))
    ax.stackplot(pd.to_datetime(daily.index), daily["everyone else"], daily["swarm candidates"],
                 colors=[GREY, RED], labels=["everyone else", "swarm candidates"])
    ax.xaxis.set_major_locator(mdates.MonthLocator())
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %Y"))
    ax.set_xlim(pd.Timestamp(daily.index.min()), pd.Timestamp(daily.index.max()))
    ax.set_ylabel("comments per day")
    ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f"{int(v):,}"))
    ax.grid(axis="y", alpha=.25)
    ax.legend(loc="upper left", fontsize=8, frameon=False)
    ax.set_title("Daily comments: candidate groups vs the rest")
    figs["daily"] = img(fig)

    gfigs = ""
    for _, g in gs.head(4).iterrows():
        m = acc[acc.group == g.group]
        s = df[(df.channel == g.channel) & df.author.isin(m.author)]
        piv = s.groupby([s.t.dt.date, s.author]).size().unstack(fill_value=0)
        fig, ax = plt.subplots(figsize=(10, 2.8))
        piv.plot(ax=ax, legend=False, lw=1)
        ax.set_title(f"Group {g.group}: comments per day, one line per account ({g.accounts} accounts, {g.channel})")
        ax.set_xlabel("day")
        ax.set_ylabel("comments")
        gfigs += img(fig)

    # ---- spam & unknown
    txt = df[(df.author != "Unknown author") & (df.text.str.len() > 40)].text.value_counts()
    spam = txt[txt >= 100].head(10)
    spam_rows = "".join(f"<tr><td>{n:,}</td><td>{esc(t[:110].replace(chr(10), ' '))}...</td></tr>" for t, n in spam.items())
    unk = df[df.author == "Unknown author"]

    def fmt(x):
        return f"{x:.0%}" if isinstance(x, float) and x <= 1 else x

    gt = gs.assign(share_of_channel=gs.share_of_channel.map("{:.0%}".format),
                   unique_text=gs.unique_text.map("{:.0%}".format), line_reuse=gs.line_reuse.map("{:.0%}".format),
                   daily_count_cv=gs.daily_count_cv.round(2), lines_per_comment=gs.lines_per_comment.round(1),
                   start=gs.start.dt.strftime("%m-%d %H:%M"), end=gs.end.dt.strftime("%m-%d %H:%M"),
                   start_spread_min=gs.start_spread_min.round(1), end_spread_min=gs.end_spread_min.round(1)
                   ).to_html(index=False, border=0)
    top_swarm = gs.iloc[0] if len(gs) else None
    plain = ""
    if top_swarm is not None:
        plain = (f"<p>The biggest candidate group is <b>{int(top_swarm.accounts)} accounts</b> in "
                 f"<b>{esc(top_swarm.channel)}</b> that wrote <b>{int(top_swarm.comments):,} comments</b> "
                 f"({top_swarm.share_of_channel:.0%} of that channel). They all began within "
                 f"{top_swarm.start_spread_min:.0f} minutes and all stopped within {top_swarm.end_spread_min:.0f} minutes "
                 f"of each other, and wrote a steady number of comments every day. "
                 f"{top_swarm.unique_text:.0%} of their comments are unique, so a copy-paste filter finds nothing, "
                 f"yet {top_swarm.line_reuse:.0%} of their sentences are re-used building blocks.</p>")
    css = ("body{font-family:-apple-system,Helvetica,Arial,sans-serif;max-width:920px;margin:0 auto;padding:24px 16px 80px;"
           "color:#1c2733;line-height:1.55}h2{margin-top:40px;border-bottom:2px solid #2f5f9e;color:#2f5f9e;padding-bottom:4px}"
           "table{border-collapse:collapse;font-size:12px;width:100%}th,td{border-bottom:1px solid #e3e8ee;padding:4px 8px;text-align:left}"
           "th{background:#f3f6fa}img{max-width:100%}.plain{background:#eef6ee;border-radius:10px;padding:8px 18px}"
           ".warn{background:#fdf0ee;border-radius:10px;padding:8px 18px}")
    page = f"""<!doctype html><html><head><meta charset="utf-8"><title>14-week data: coordinated groups</title><style>{css}</style></head><body>
<h1>14-week export: who acts together?</h1>
<p>{len(df):,} comments, {df.channel.nunique()} channels, {df.author.nunique():,} author names,
{df.t.min():%d %b %Y} to {df.t.max():%d %b %Y}. Rule: heavy accounts (&ge;{a.min_comments} comments) in one channel whose
active window starts <i>and</i> ends within {a.tol_min} minutes of each other, at least 3 accounts.</p>
<div class="warn"><b>Caution:</b> this is behaviour, not identity. A group acting together is not proof of bots,
and the report names real Telegram accounts: do not commit or share it publicly.</div>
<h2>1. Shape of the data</h2>{figs['weeks']}{figs['chan']}
<h2>2. Candidate groups</h2>{gt}{figs['daily']}
<h2>3. Inside the largest groups</h2>{gfigs}
<h2>4. Not a swarm: commercial spam and unknown authors</h2>
<p>Texts repeated at least 100 times (ads, USDT/crypto offers). Different problem from narrative swarms; filter before clustering.</p>
<table><tr><th>copies</th><th>text</th></tr>{spam_rows}</table>
<p>{len(unk):,} comments have no author ('Unknown author'): {unk.channel.value_counts().head(3).to_dict()}.
They cannot be linked to accounts, so they stay out of group detection.</p>
<h2>5. In plain English</h2><div class="plain">{plain}
<p>Real people do not comment at an even rate day and night, start the same minute as strangers, and stop the same minute.
That shared schedule is the evidence; the wording is not.</p>
<p>Limits: only accounts with a visible name are grouped, the rule needs heavy posters (quiet swarms are missed), and a
channel running a genuine campaign with volunteers could look similar. A reviewer decides.</p></div>
</body></html>"""
    (out / "weeks14_report.html").write_text(page, encoding="utf-8")
    print(f"{len(gs)} candidate groups, {int(gs.accounts.sum()) if len(gs) else 0} accounts, "
          f"{int(gs.comments.sum()) if len(gs) else 0:,} comments -> {out / 'weeks14_report.html'}")
    print(gs[["group", "channel", "accounts", "comments", "start_spread_min", "end_spread_min", "unique_text",
              "daily_count_cv", "line_reuse"]].head(10).round(2).to_string(index=False))


if __name__ == "__main__":
    main()
