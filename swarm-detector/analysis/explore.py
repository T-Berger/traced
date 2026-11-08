"""Explore the Swarm Detector dataset and write explore_report.html.

Run:  python explore.py   (needs pandas + matplotlib)
Sections run technical -> simple: schema, NaNs, one row, relations, signals, plain-English summary.
"""
import base64, gzip, html, io, json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

HERE = Path(__file__).resolve().parent
DATA = HERE.parent / "data"
OUT = HERE / "explore_report.html"

plt.rcParams.update({"figure.dpi": 130, "axes.spines.top": False, "axes.spines.right": False,
                     "font.size": 9, "axes.titleweight": "bold", "axes.titlesize": 10})
BLUE, RED, GREY = "#2f5f9e", "#c0392b", "#9aa5b1"

items = pd.read_csv(DATA / "items.csv.gz", low_memory=False)
analyses = pd.read_csv(DATA / "analyses.csv.gz", low_memory=False)
meta = json.load(gzip.open(DATA / "metadata_clean.json.gz", "rt"))
meta_ids = {x["id"] for x in meta["items"]}
export_info = meta["export"]
del meta

items["published"] = pd.to_datetime(items.published_at, utc=True)
comments = items[items.type == "SOCIAL_MEDIA_COMMENT"].copy()
posts = items[items.type == "SOCIAL_MEDIA_POST"].copy()
media = items[~items.type.isin(["SOCIAL_MEDIA_COMMENT", "SOCIAL_MEDIA_POST"])]


def fig_html(fig, alt):
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight")
    plt.close(fig)
    return f'<img alt="{alt}" src="data:image/png;base64,{base64.b64encode(buf.getvalue()).decode()}">'


def table(df, index=True, cls=""):
    return df.to_html(index=index, classes=cls, border=0, escape=True, na_rep="NaN")


def esc(x):
    return html.escape(str(x))


# ---------- numbers ----------
n_items, n_cols = items.shape
type_counts = items.type.value_counts()
chan = comments.source_name.value_counts()
authors = comments.name_orig.value_counts()
parent_ids = set(items.id)
cmt_parent = comments.parent_id.dropna().astype("int64")
parent_found = cmt_parent.isin(parent_ids).mean()
parent_types = items.set_index("id").type.reindex(cmt_parent).value_counts(dropna=False)
text = comments.description_orig
placeholder = (text == "No description").sum()
dup_share = text.duplicated(keep=False).mean()
multi_chan = comments.groupby("name_orig").source_name.nunique()
cross = (multi_chan > 1).sum()
same_sec = comments.dropna(subset=["published"]).groupby(["parent_id", comments.published.dt.floor("s")]).size()
text_len = text.str.len()
handle_like = comments.name_orig.str.match(r"^[A-Za-z0-9_]{4,}\s").mean()

# ---------- charts ----------
charts = {}

fig, ax = plt.subplots(figsize=(6, 2.6))
miss = items.isna().mean().sort_values()
miss = miss[miss > 0] * 100
ax.barh(miss.index, miss.values, color=RED)
for i, v in enumerate(miss.values):
    ax.text(v + 0.3, i, f"{v:.1f}%", va="center")
ax.set_xlabel("% of rows missing (NaN)")
ax.set_title("Which columns have gaps (all 178k rows)")
charts["nan"] = fig_html(fig, "missing values")

fig, ax = plt.subplots(figsize=(6, 2.4))
ax.barh(type_counts.index[::-1], type_counts.values[::-1], color=BLUE)
ax.set_xscale("log")
for i, v in enumerate(type_counts.values[::-1]):
    ax.text(v * 1.1, i, f"{v:,}", va="center")
ax.set_title("What kinds of rows (log scale)")
charts["types"] = fig_html(fig, "row types")

fig, ax = plt.subplots(figsize=(6, 3.6))
c15 = chan[::-1]
ax.barh(c15.index, c15.values, color=BLUE)
ax.set_title("Comments per channel")
charts["chan"] = fig_html(fig, "comments per channel")

fig, ax = plt.subplots(figsize=(6, 2.6))
hourly = comments.dropna(subset=["published"]).set_index("published").resample("1h").size()
ax.fill_between(hourly.index, hourly.values, color=BLUE, alpha=.8)
ax.set_title("Comments per hour (UTC), 13-21 Oct 2026")
fig.autofmt_xdate()
charts["hourly"] = fig_html(fig, "comments per hour")

fig, axs = plt.subplots(1, 2, figsize=(6.4, 2.6))
axs[0].hist(authors.values, bins=[1, 2, 3, 5, 10, 20, 50, 100, 2000], color=BLUE)
axs[0].set_xscale("log"); axs[0].set_title("Comments per author name"); axs[0].set_xlabel("comments (log)")
axs[1].hist(text_len.clip(upper=400), bins=40, color=BLUE)
axs[1].set_title("Comment length (chars, capped 400)")
fig.tight_layout()
charts["dist"] = fig_html(fig, "distributions")

fig, ax = plt.subplots(figsize=(6, 3))
top = authors.head(15)[::-1]
ax.barh([n[:28] for n in top.index], top.values, color=RED)
ax.set_title("15 most active author names")
charts["top"] = fig_html(fig, "top authors")

lang = (comments.assign(language=comments.language.fillna("(missing)"))
        .groupby(["source_name", "language"]).size().unstack(fill_value=0))
lang = lang[[c for c in ["ru", "uk", "en", "kk", "(missing)"] if c in lang]]
lang = lang.loc[chan.index]
fig, ax = plt.subplots(figsize=(6, 3.6))
(lang.div(lang.sum(axis=1), axis=0) * 100).iloc[::-1].plot.barh(stacked=True, ax=ax, color=[RED, BLUE, "#6aa84f", "#e69138", GREY], width=.8)
ax.set_title("Comment language by channel (%)"); ax.legend(ncol=5, fontsize=7, loc="upper center", bbox_to_anchor=(.5, -.12))
charts["lang"] = fig_html(fig, "language by channel")

# ---------- single row ----------
row = comments.iloc[0]
explain = {
    "id": "Unique item id", "type": "Comment, post, image, video...", "file_type": "MIME type of the stored file",
    "source_id": "Channel id", "source_name": "Telegram channel the item lives in",
    "original_source_url": "Link to the message on Telegram", "parent_id": "Id of the item this comment replies to: a post OR another comment (empty for posts)",
    "language": "Detected language (often empty)", "published_at": "When it was posted (UTC)",
    "created_at": "When the platform scraped it", "size_bytes": "Size of the text file",
    "name_orig": "AUTHOR display name as typed by the user (the only account-like field!)",
    "name_de": "German translation of the name", "name_en": "English translation of the name",
    "description_orig": "The comment TEXT, original language", "description_de": "German translation of the text",
    "description_en": "English translation of the text", "analysis_count": "Number of derived analyses attached",
    "filename_in_zip": "Path inside the original export (not needed)"}
row_rows = "".join(
    f"<tr><td><code>{esc(k)}</code></td><td>{'<span class=nan>NaN</span>' if pd.isna(v) else esc(v)}</td><td>{esc(explain.get(k, ''))}</td></tr>"
    for k, v in row.items() if k != "published")
sample = comments[["source_name", "name_orig", "published_at", "language", "description_orig", "description_en"]].sample(8, random_state=3)
sample["description_orig"] = sample.description_orig.str[:90]
sample["description_en"] = sample.description_en.str[:90]

dtypes = pd.DataFrame({"dtype": items.drop(columns="published").dtypes.astype(str),
                       "non-null": items.drop(columns="published").notna().sum(),
                       "NaN": items.drop(columns="published").isna().sum(),
                       "NaN %": (items.drop(columns="published").isna().mean() * 100).round(1),
                       "distinct": items.drop(columns="published").nunique()})
# NaN by type matrix
nan_by_type = items.drop(columns="published").groupby(items.type).apply(lambda g: (g.isna().mean() * 100).round(0)).T
nan_by_type = nan_by_type.loc[(nan_by_type > 0).any(axis=1)]

an = analyses.groupby("analysis_type").agg(rows=("analysis_id", "size"), items=("item_id", "nunique"))
an_items_in = analyses.item_id.isin(parent_ids).mean()

# ---------- html ----------
top_name = authors.index[0]
css = """
body{font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;max-width:900px;margin:0 auto;padding:24px 16px 80px;color:#1c2733;line-height:1.55;background:#fff}
h1{font-size:28px;margin:0 0 4px} h2{margin-top:44px;border-bottom:2px solid #2f5f9e;padding-bottom:4px;color:#2f5f9e}
h3{margin-top:26px} .sub{color:#667;margin-bottom:24px}
table{border-collapse:collapse;font-size:12.5px;margin:10px 0;width:100%} th,td{border-bottom:1px solid #e3e8ee;padding:5px 8px;text-align:left;vertical-align:top}
th{background:#f3f6fa} code{background:#f0f3f7;padding:1px 5px;border-radius:4px;font-size:12px}
img{max-width:100%;margin:8px 0} .nan{color:#c0392b;font-weight:600}
.kpi{display:flex;gap:12px;flex-wrap:wrap;margin:14px 0}.kpi div{flex:1;min-width:130px;background:#f3f6fa;border-radius:8px;padding:10px 12px}
.kpi b{display:block;font-size:22px;color:#2f5f9e}.kpi span{font-size:12px;color:#556}
.tech{border-left:4px solid #2f5f9e;padding-left:12px;color:#334} .plain{background:#eef6ee;border-radius:10px;padding:14px 18px;margin:12px 0}
.warn{background:#fdf0ee;border-radius:10px;padding:12px 18px;margin:12px 0}
.tag{display:inline-block;font-size:11px;background:#2f5f9e;color:#fff;border-radius:10px;padding:1px 9px;margin-left:8px;vertical-align:middle}
.tag.s{background:#4c9a4c}
"""

h = f"""<!doctype html><html><head><meta charset="utf-8"><title>Swarm Detector - Data Exploration</title><style>{css}</style></head><body>
<h1>Swarm Detector: data exploration</h1>
<div class="sub">15 Telegram war channels, 14-21 Oct 2026 &middot; technical first (parts 1-6), plain English last (part 7)</div>

<div class="kpi">
<div><b>{n_items:,}</b><span>rows in items.csv.gz</span></div>
<div><b>{len(comments):,}</b><span>comments</span></div>
<div><b>{len(posts):,}</b><span>posts</span></div>
<div><b>{len(authors):,}</b><span>distinct author names</span></div>
<div><b>{len(analyses):,}</b><span>rows in analyses.csv.gz</span></div>
</div>

<h2>1. Files and shape <span class="tag">technical</span></h2>
<table><tr><th>file</th><th>shape</th><th>what it is</th></tr>
<tr><td><code>items.csv.gz</code></td><td>{n_items:,} rows x {n_cols} cols</td><td>One row per Telegram message or media file. <b>This is the file that matters.</b></td></tr>
<tr><td><code>analyses.csv.gz</code></td><td>{analyses.shape[0]:,} rows x {analyses.shape[1]} cols</td><td>Index of derived outputs (translations, OCR, transcripts, word counts). Only file <i>names</i>, not the content; {an_items_in*100:.0f}% of rows point at an existing item.</td></tr>
<tr><td><code>metadata_clean.json.gz</code></td><td>{len(meta_ids):,} items</td><td>Same items as nested JSON. Ids match items.csv: <b>{meta_ids == parent_ids}</b>. Export: project {export_info['filter']['project_id']}, {export_info['filter']['start'][:10]} to {export_info['filter']['end_exclusive'][:10]}.</td></tr></table>
<div class="tech">Note: <code>wc -l</code> shows 420k lines for items.csv because comment texts contain line breaks. Always parse with a CSV reader; the true row count is {n_items:,}.</div>
<h3>Row types</h3>{charts['types']}
<h3>Columns, dtypes, NaNs</h3>{table(dtypes)}

<h2>2. Missing values (NaN) <span class="tag">technical</span></h2>
{charts['nan']}
<h3>NaN % by row type</h3>{table(nan_by_type)}
<ul>
<li><code>parent_id</code> is empty for posts by design ({posts.parent_id.isna().mean()*100:.0f}% of posts) and for {comments.parent_id.isna().sum():,} comments ({comments.parent_id.isna().mean()*100:.1f}%): <b>orphan comments</b> we cannot attach to a post.</li>
<li><code>published_at</code> is empty for {items.published.isna().sum():,} rows, <b>only media</b> (images/video/audio; ~94% of them). Comments always have a timestamp, to the second.</li>
<li><code>language</code> is empty for {comments.language.isna().mean()*100:.0f}% of comments, and the translated columns <code>*_en</code>/<code>*_de</code> are empty for ~{comments.description_en.isna().mean()*100:.0f}%. Typically emoji-only or very short texts. Fall back to <code>description_orig</code>.</li>
<li>Resolvable parents: of comments with a <code>parent_id</code>, {parent_found*100:.1f}% point at an id present in the file. <b>Replies are threaded:</b> more comments answer other comments than answer posts. Parent types: {', '.join(f'{k}: {v:,}' for k, v in parent_types.items())}.</li>
</ul>

<h2>3. One single row <span class="tag">technical</span></h2>
<p>A comment, exactly as stored (first comment in the file):</p>
<table><tr><th>column</th><th>value</th><th>meaning</th></tr>{row_rows}</table>
<h3>Eight random comments</h3>{table(sample, index=False)}

<h2>4. The important catch: there is no account id <span class="tag">technical</span></h2>
<div class="warn"><b>The only account-like field is <code>name_orig</code></b>, a free-text display name. No user id, no username column. {handle_like*100:.0f}% of names look like <code>handle DisplayName</code> (e.g. <code>{esc(top_name)}</code>), so the handle is often embedded; others are just first names such as "Сергей" or ".". Different people can share a name, and one person can use several names. Any swarm result built on it is a <i>name-level</i> inference.</div>
<ul>
<li>{len(authors):,} distinct names across {len(comments):,} comments. Median {authors.median():.0f} comments per name; {(authors==1).mean()*100:.0f}% of names appear exactly once.</li>
<li>Heavy tail: the top name posted {authors.iloc[0]:,} comments in 7 days; top 10 names produced {authors.head(10).sum()/len(comments)*100:.1f}% of all comments.</li>
<li>Names active in more than one channel: <b>{cross:,}</b> ({cross/len(authors)*100:.1f}%). These matter for the "both sides" requirement.</li>
</ul>
{charts['top']}{charts['dist']}

<h2>5. Content quality <span class="tag">technical</span></h2>
<ul>
<li>Exact duplicate comment texts: {dup_share*100:.1f}% of comments share their text with another comment. Part is noise: <code>No description</code> ({placeholder:,}x), pinned channel rules, donation banners, laughing emoji. These must be filtered before using text similarity or you will "find" harmless swarms.</li>
<li>Median comment length {text_len.median():.0f} chars; 75% under {text_len.quantile(.75):.0f}; max {text_len.max():,}.</li>
<li>Same parent post and same second: {int((same_sec>1).sum()):,} (post, second) buckets hold 2+ comments. Max {int(same_sec.max())} comments in one second on one post: a raw timing signal to test.</li>
</ul>
{charts['lang']}

<h2>6. Where and when <span class="tag">technical</span></h2>
{charts['chan']}{charts['hourly']}
<div class="tech">Channels are heavily skewed: {chan.index[0]} alone has {chan.iloc[0]/len(comments)*100:.0f}% of comments; the top 3 have {chan.head(3).sum()/len(comments)*100:.0f}%. Small channels will need per-channel normalisation. Day 13 Oct is a partial day (1,293 rows), so effective window is 14-21 Oct.</div>

<h2>7. In plain English <span class="tag s">simple</span></h2>
<div class="plain">
<p><b>What we have:</b> a week of reader comments under posts of 15 Telegram channels about the war, about {len(comments)//1000}k comments. Each comes with who wrote it (a name only), when (to the second), which channel and which message it answers, plus an English and German translation.</p>
<p><b>What is great:</b> exact timing, a clear link from each comment to what it replies to, and ready translations. We can ask "who always shows up together, at the same moment, saying the same thing?"</p>
<p><b>What is tricky:</b></p>
<ul>
<li>There are <b>no real account ids</b>, only display names. Two "Сергей" might be two people.</li>
<li>About 1 in 14 comments is a copy of another, but many are harmless: emoji, channel rules, donation banners. We must clean these out first.</li>
<li>Some data is missing: language on about 1 in 6 comments, a few comments without their parent, and no timestamps on pictures and videos.</li>
<li>Activity is lopsided: one channel holds 40% of all comments and a handful of names write over a thousand comments each. A lively fan is not a swarm.</li>
</ul>
<p><b>What this means for the challenge:</b> we cannot say "this is a bot". We can say "these names keep appearing together, within seconds, with similar words, in several channels", then keep real, angry or very active people out of the list.</p>
</div>
</body></html>"""
OUT.write_text(h, encoding="utf-8")
print("wrote", OUT, f"{OUT.stat().st_size/1e6:.1f} MB")
