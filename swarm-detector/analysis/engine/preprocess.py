"""Proposed export preprocessing; account analysis is currently unavailable.

``run`` fails before deriving accounts while source identities are unverified.

Every step is recorded as a `Step` (rows in, rows out, what changed and why) so the
preprocessing report is generated from the same code that does the work.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from urllib.parse import urlparse

import numpy as np
import pandas as pd

from . import config as cfg
from . import readiness

COMMENT, POST = "SOCIAL_MEDIA_COMMENT", "SOCIAL_MEDIA_POST"

INVISIBLE = re.compile("[­​-‏‪-‮⁠-⁤﻿]")
HANDLE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,31}$")
URL = re.compile(r"(?:https?://|www\.)\S+|\b(?:t\.me|[\w-]+\.(?:com|ru|ua|org|net|me|io|info))/\S*", re.I)
MENTION = re.compile(r"@\w+")
NON_WORD = re.compile(r"[^\w\s]|_")
SPACES = re.compile(r"\s+")


@dataclass
class Step:
    name: str
    kind: str  # "filter" removes rows, "transform" adds or rewrites columns, "flag" marks rows
    rows_in: int
    rows_out: int
    detail: str
    why: str


@dataclass
class Preprocessed:
    comments: pd.DataFrame  # clean audience comments (service messages removed)
    service: pd.DataFrame  # removed channel service messages, kept for the report
    steps: list[Step] = field(default_factory=list)
    facts: dict = field(default_factory=dict)


def clean_name(name: str) -> str:
    name = unicodedata.normalize("NFKC", str(name))
    return SPACES.sub(" ", INVISIBLE.sub("", name)).strip()


def split_handle(name: str) -> tuple[str | None, str]:
    """Return (handle, quality). Telegram usernames are 5-32 chars of [A-Za-z0-9_].

    strong:   first token looks like a username (digit, underscore or camelCase in it)
    probable: first token is a lowercase Latin word followed by a different (non-ASCII) name
    name:     no usable handle; the display name is the only identity
    """
    tokens = name.split(" ")
    first = tokens[0]
    if not HANDLE.match(first):
        return None, "name"
    if any(ch.isdigit() for ch in first) or "_" in first or re.search(r"[a-z][A-Z]", first):
        return first.lower(), "strong"
    rest = " ".join(tokens[1:])
    if len(tokens) > 1 and first.islower() and not rest.isascii():
        return first.lower(), "probable"
    return None, "name"


def text_key(text: str) -> str:
    """Comparison key: casefolded words and digits only (no URLs, mentions, emoji, punctuation)."""
    text = unicodedata.normalize("NFKC", text).casefold()
    text = MENTION.sub(" ", URL.sub(" ", INVISIBLE.sub("", text)))
    return SPACES.sub(" ", NON_WORD.sub(" ", text)).strip()


def domains(text: str) -> str:
    out = []
    for u in URL.findall(text):
        host = urlparse(u if "://" in u else "http://" + u).netloc.lower().removeprefix("www.")
        if host:
            out.append(host)
    return " ".join(out)


def resolve_threads(c: pd.DataFrame, items: pd.DataFrame) -> pd.DataFrame:
    """Walk parent links up to the root post. Replies can answer comments, so a chain is followed."""
    kind = dict(zip(items.id, items.type, strict=True))
    parent = dict(zip(items.id, items.parent_id.astype("Int64"), strict=True))
    cur = c.parent_id.astype("Int64").copy()
    root = pd.Series(pd.NA, index=c.index, dtype="Int64")
    depth = cur.notna().astype(int)
    for _ in range(100):
        t = cur.map(kind)
        hit = (t == POST) & root.isna()
        root[hit] = cur[hit]
        walk = (t == COMMENT) & root.isna()
        if not walk.any():
            break
        cur = cur.where(~walk, cur.map(parent).astype("Int64"))
        depth += walk.astype(int)
    out = c.assign(root_id=root, depth=depth.where(root.notna()))
    out["parent_type"] = c.parent_id.astype("Int64").map(kind).fillna("missing")
    return out


def run() -> Preprocessed:
    # No declared source-native sender identity mapping exists for this export.
    # Never collapse records by a display name or an inferred handle.
    readiness.require_source_identity()
    steps: list[Step] = []
    facts: dict = {}

    items = pd.read_csv(cfg.DATA / "items.csv.gz", low_memory=False)
    items["t"] = pd.to_datetime(items.published_at, utc=True, errors="coerce")
    n0 = len(items)
    steps.append(Step("Load items.csv.gz", "filter", n0, n0,
                      f"{n0:,} rows, {items.shape[1] - 1} columns, {items.source_name.nunique()} channels.",
                      "Starting point. One row per Telegram message or attached media file."))

    c = items[items.type == COMMENT].copy()
    by_type = items.type.value_counts()
    steps.append(Step("Keep comments only", "filter", n0, len(c),
                      ", ".join(f"{k.replace('SOCIAL_MEDIA_', '').lower()}: {v:,}" for k, v in by_type.items()
                                if k != COMMENT) + " set aside. Posts are kept as thread roots.",
                      "Swarms act in the comments. Posts are the channel's own messages and media rows are "
                      "attachments, not separate acts."))

    n = len(c)
    c = c[c.t.notna()]
    facts["time_range"] = (c.t.min(), c.t.max())
    steps.append(Step("Parse timestamps (UTC)", "filter", n, len(c),
                      f"published_at parsed to UTC; {n - len(c):,} unparseable dropped. "
                      f"Range {c.t.min():%Y-%m-%d %H:%M} to {c.t.max():%Y-%m-%d %H:%M} UTC.",
                      "Every timing metric needs a valid, single time zone."))

    # --- authors
    n = len(c)
    c["name_clean"] = c.name_orig.map(clean_name)
    hq = c.name_clean.map(split_handle)
    c["handle"] = hq.str[0]
    c["id_quality"] = hq.str[1]
    c["account"] = np.where(c.handle.notna(), "@" + c.handle.fillna(""), "~" + c.name_clean.str.casefold())
    facts["names_raw"] = c.name_orig.nunique()
    facts["names_clean"] = c.name_clean.nunique()
    facts["accounts"] = c.account.nunique()
    facts["id_quality_rows"] = c.id_quality.value_counts()
    facts["id_quality_accounts"] = c.groupby("id_quality").account.nunique()
    merged = c.groupby("account").name_orig.nunique()
    facts["merged_examples"] = (c[c.account.isin(merged[merged > 1].index)]
                                .groupby("account").name_orig.unique().head(8))
    facts["accounts_multi_name"] = int((merged > 1).sum())
    steps.append(Step("Normalise author identity", "transform", n, n,
                      f"{facts['names_raw']:,} raw names -> {facts['names_clean']:,} after Unicode NFKC and removing "
                      f"invisible characters -> {facts['accounts']:,} accounts after keying on the Telegram handle "
                      f"where one is embedded. {facts['accounts_multi_name']:,} accounts appear under 2+ raw names.",
                      "The data has no user id. A handle is stable while display names change, so the handle is "
                      "the key when present; otherwise the cleaned, casefolded display name."))

    n = len(c)
    c = c.drop_duplicates(["account", "parent_id", "t", "description_orig"])
    steps.append(Step("Drop exact duplicate rows", "filter", n, len(c),
                      f"Same account, same parent, same second, same text: {n - len(c):,} rows.",
                      "Export artefacts would otherwise look like copy-paste and inflate counts."))

    # --- text
    n = len(c)
    raw = c.description_orig.fillna("")
    c["is_media_only"] = raw.isin(["", "No description"])
    c["text"] = raw.where(~c.is_media_only, "")
    c["text_key"] = c.text.map(text_key)
    c["text_len"] = c.text_key.str.len()
    c["domains"] = c.text.map(domains)
    c["n_urls"] = c.domains.str.split().str.len().fillna(0).astype(int)
    c["is_low_info"] = ~c.is_media_only & (c.text_len < 3)
    facts["text_flags"] = {
        "media only (no text)": int(c.is_media_only.sum()),
        "low information (emoji, '+', '?')": int(c.is_low_info.sum()),
        "with a link": int((c.n_urls > 0).sum()),
    }
    steps.append(Step("Normalise text", "flag", n, n,
                      f"NFKC + casefold, URLs and @mentions removed, punctuation and emoji stripped -> text_key. "
                      f"{c.is_media_only.sum():,} media-only comments ('No description') and "
                      f"{c.is_low_info.sum():,} low-information comments flagged, not dropped.",
                      "Two comments are 'the same text' only if they match after formatting is removed. Media-only "
                      "and emoji comments still carry timing, so they stay for timing metrics."))

    # --- service bots. Telegram requires every bot username to end in "bot", so this is exact for
    # accounts that show their handle. A repeated-template rule was tried first and also caught a
    # human repeating an insult 11 times, which is behaviour we want to keep.
    n = len(c)
    is_service = c.handle.fillna("").str.endswith("bot")
    service = c[is_service].copy()
    c = c[~is_service].copy()
    facts["service_accounts"] = (service.groupby("name_clean")
                                 .agg(rows=("id", "size"), channel=("source_name", "first"),
                                      example=("text", lambda s: s.mode().iloc[0] if len(s.mode()) else ""))
                                 .sort_values("rows", ascending=False))
    rep = c[c.text_len >= cfg.DUP_MIN_CHARS].groupby("text_key").agg(rows=("id", "size"),
                                                                     accounts=("account", "nunique"))
    facts["copy_paste_texts"] = int((rep.accounts >= 2).sum())
    facts["self_repeat_texts"] = int(((rep.rows >= 3) & (rep.accounts == 1)).sum())
    steps.append(Step("Remove Telegram service bots", "filter", n, len(c),
                      f"{service.account.nunique()} accounts whose handle ends in 'bot' (moderation, donation and "
                      f"welcome bots): {len(service):,} rows. Kept for the signals: {facts['copy_paste_texts']:,} "
                      f"texts of {cfg.DUP_MIN_CHARS}+ characters posted by 2+ accounts, "
                      f"{facts['self_repeat_texts']:,} texts one account repeats 3+ times.",
                      "Channel bots are automated but not a swarm; left in, they would top every 'bot-like' "
                      "ranking. Telegram forces bot usernames to end in 'bot', so the rule is exact."))

    # --- authors that are not individuals
    n = len(c)
    placeholder = c.name_clean.str.casefold().isin(cfg.PLACEHOLDER_NAMES)
    chat = c.name_clean.str.contains(cfg.CHAT_AS_AUTHOR, regex=True)
    facts["non_individual"] = (c[placeholder | chat].assign(reason=np.where(placeholder[placeholder | chat],
                                                                           "placeholder", "chat posting as itself"))
                               .groupby(["name_clean", "reason"]).size().rename("rows").reset_index()
                               .sort_values("rows", ascending=False))
    c = c[~(placeholder | chat)].copy()
    steps.append(Step("Remove authors that are not one person", "filter", n, len(c),
                      f"Placeholders ('Unknown author', 'Deleted Account', anonymous 'Admin'): "
                      f"{int(placeholder.sum()):,} rows; a channel's chat posting as itself: "
                      f"{int((chat & ~placeholder).sum()):,} rows.",
                      "A placeholder merges many unrelated people into one fake 'account' that would look "
                      "hyperactive and present everywhere."))
    one_word = (c.id_quality == "name") & ~c.name_clean.str.contains(" ")
    facts["generic_share"] = float(one_word.mean())
    facts["generic_top"] = c[one_word].name_clean.value_counts().head(12)

    # --- threads
    n = len(c)
    c = resolve_threads(c, items)
    post_t = items[items.type == POST].set_index("id").t
    any_t = items.set_index("id").t
    c["root_t"] = c.root_id.map(post_t)
    c["latency_post_s"] = (c.t - c.root_t).dt.total_seconds()
    c["latency_parent_s"] = (c.t - c.parent_id.astype("Int64").map(any_t)).dt.total_seconds()
    neg = int((c.latency_post_s < 0).sum())
    c.loc[c.latency_post_s < 0, "latency_post_s"] = np.nan
    c.loc[c.latency_parent_s < 0, "latency_parent_s"] = np.nan
    facts["parent_types"] = c.parent_type.value_counts()
    facts["depth"] = c.depth.value_counts().sort_index()
    facts["root_found"] = float(c.root_id.notna().mean())
    facts["negative_latency"] = neg
    steps.append(Step("Resolve threads to the root post", "transform", n, n,
                      f"Followed parent links (reply to reply to post). Root post found for "
                      f"{c.root_id.notna().mean() * 100:.1f}% of comments; the rest are orphans (no parent or parent "
                      f"outside the export). {neg:,} comments dated before their post set to unknown latency.",
                      "'Same post' and 'time since post' are the two core coordination signals; replies to "
                      "replies must still count as being on the same post."))

    # --- time fields
    local = c.t.dt.tz_convert(cfg.LOCAL_TZ)
    c["hour"] = local.dt.hour
    c["dow"] = local.dt.dayofweek
    c["side"] = c.source_name.map(cfg.CHANNEL_SIDE).fillna("unknown")
    c = c.sort_values(["account", "t"])
    c["gap_prev_s"] = c.groupby("account").t.diff().dt.total_seconds()
    steps.append(Step("Derive time and side fields", "transform", len(c), len(c),
                      f"Local hour and weekday ({cfg.LOCAL_TZ}), seconds since the account's previous comment, "
                      f"channel side (ua / ru, our labelling).",
                      "Inputs for circadian, rhythm and cross-side metrics."))

    # --- activity threshold
    n = len(c)
    per_acc = c.account.value_counts()
    keep = per_acc[per_acc >= cfg.MIN_COMMENTS].index
    c["in_model"] = c.account.isin(keep)
    facts["per_account"] = per_acc
    steps.append(Step(f"Accounts with {cfg.MIN_COMMENTS}+ comments enter the models", "filter", n,
                      int(c.in_model.sum()),
                      f"{len(keep):,} of {len(per_acc):,} accounts ({len(keep) / len(per_acc) * 100:.1f}%) hold "
                      f"{c.in_model.mean() * 100:.1f}% of comments. Smaller accounts stay in the table for pair "
                      f"and text signals.",
                      "A median or entropy from 1-4 comments is mostly noise; rare accounts still show up as the "
                      "partners of active ones."))

    c = c.drop(columns=["name_de", "name_en", "description_de", "filename_in_zip", "analysis_count",
                        "created_at", "file_type", "original_source_url", "type", "published_at", "size_bytes"])
    c = c.rename(columns={"source_name": "channel"}).sort_values("t").reset_index(drop=True)
    facts["channels"] = c.groupby(["channel", "side"]).size().rename("comments").reset_index()
    return Preprocessed(c, service, steps, facts)
