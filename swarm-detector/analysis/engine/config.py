"""Paths and analyst choices shared by every stage. Each choice is shown in the reports."""

from pathlib import Path

ANALYSIS = Path(__file__).resolve().parents[1]
DATA = ANALYSIS.parent / "data"
CACHE = ANALYSIS / "cache"
RESULTS = ANALYSIS / "results"

LOCAL_TZ = "Europe/Kyiv"

# Channel side. Our own labelling from public knowledge of the channels, not given in the data.
# @rezident_ua writes for a Ukrainian audience but is widely reported as Russian-run; we count it
# as "ua" (where it publishes) and say so in the report.
CHANNEL_SIDE = {
    "@DeepStateUA": "ua",
    "@dmytrogordon_official": "ua",
    "@ssternenko": "ua",
    "@vanek_nikolaev": "ua",
    "@rezident_ua": "ua",
    "@RVvoenkor": "ru",
    "@Sladkov_plus": "ru",
    "@Warhronika": "ru",
    "@boris_rozhin": "ru",
    "@dva_majors": "ru",
    "@rybar": "ru",
    "@sashakots": "ru",
    "@voenacher": "ru",
    "@wargonzo": "ru",
    "@yurasumy": "ru",
}

# Author fields that are not one individual: Telegram placeholders for hidden or deleted senders,
# an anonymous group admin, and a channel's chat posting as itself.
PLACEHOLDER_NAMES = {"unknown author", "unknown", "deleted account", "deleted", "удалённый аккаунт",
                     "удаленный аккаунт", "видалений акаунт", "admin"}
CHAT_AS_AUTHOR = r"(?i)(?:\bчат\b|чатик|\bchat\b|^chat)"

# Minimum comments for an account to get behavioural metrics (a median of 2 values means little).
MIN_COMMENTS = 5

# Minimum normalised text length for duplicate-text metrics ("+" or "да" are not copy-paste).
DUP_MIN_CHARS = 15

# Co-activity: two accounts are "together" on a post when they comment on the same root post
# within this many seconds of each other.
COACTIVITY_WINDOW_S = 300

FAST_REPLY_S = 120
NIGHT_HOURS = range(1, 6)  # 01:00-05:59 Kyiv time

SEED = 7
