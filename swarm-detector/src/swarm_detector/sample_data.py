"""Fictional demonstration data: no real accounts, statements, or allegations."""

import random
from datetime import UTC, datetime, timedelta


def build_sample() -> dict:
    """Return a deterministic, explicitly synthetic investigation fixture."""
    rng = random.Random(9472)
    dataset = {
        "dataset_id": "synthetic-demonstration-v1",
        "label": "Synthetic demonstration — fictional accounts and discussions",
        "synthetic": True,
        "channels": [
            {
                "channel_key": "syn-channel-a",
                "name": "Synthetic channel A · civic news",
                "category": "Ukrainian news or civic discussion",
                "rationale": "Fictional Ukrainian civic-news setting for the synthetic demonstration.",
            },
            {
                "channel_key": "syn-channel-b",
                "name": "Synthetic channel B · local discussion",
                "category": "Ukrainian news or civic discussion",
                "rationale": "Fictional Ukrainian discussion setting for the synthetic demonstration.",
            },
            {
                "channel_key": "syn-channel-c",
                "name": "Synthetic channel C · military commentary",
                "category": "pro-Kremlin commentary",
                "rationale": "Assigned fictional scenario category; this is not a claim about a real channel.",
            },
        ],
        "posts": [],
        "messages": [],
        "comparisons": [],
        "manifest": {
            "synthetic": True,
            "description": "All records are generated fixtures. Recovery demonstrates pipeline behaviour, not real-world accuracy.",
            "identity_stable_across_channels": True,
            "timestamp_precision": "seconds",
            "timestamp_precision_seconds": 1,
            "coverage": "Fictional text-only discussions across three channels; no real Telegram data.",
        },
    }
    start = datetime(2026, 10, 14, 8, tzinfo=UTC)
    repeat_accounts = [f"syn-repeat-{i:02d}" for i in range(1, 9)]
    volunteer_accounts = [f"syn-volunteer-{i:02d}" for i in range(1, 6)]
    background = [f"syn-background-{i:03d}" for i in range(1, 91)]
    message_number = 0

    def iso(timestamp):
        return timestamp.isoformat().replace("+00:00", "Z")

    def add_post(key, channel, timestamp, title, text=None, family=None):
        dataset["posts"].append(
            {
                "post_key": key,
                "channel_key": channel,
                "post_family_key": family or key,
                "timestamp_utc": iso(timestamp),
                "title": "[Synthetic] " + title,
                "text_original": text or "Synthetic discussion prompt: " + title,
                "source_ref": "synthetic://posts/" + key,
            }
        )

    def add_message(post, channel, account, timestamp, text, urls=None):
        nonlocal message_number
        message_number += 1
        key = f"syn-message-{message_number:05d}"
        dataset["messages"].append(
            {
                "message_key": key,
                "post_key": post,
                "account_key": account,
                "channel_key": channel,
                "timestamp_utc": iso(timestamp),
                "text_original": "[Synthetic] " + text,
                "urls": urls or [],
                "source_ref": "synthetic://messages/" + key,
                "sender_type": "user",
            }
        )

    # Each hour has unrelated posts and a varied audience. Sender-label controls
    # can therefore rearrange participation without deleting the target burst.
    for episode in range(15):
        channel = f"syn-channel-{'abc'[episode % 3]}"
        hour = start + timedelta(days=episode // 3, hours=(episode % 3) * 2)
        actors = repeat_accounts if episode < 9 else volunteer_accounts
        key = f"syn-episode-{episode + 1:02d}"
        activism = episode >= 9
        title = (
            f"Public volunteer response #{episode - 8}"
            if activism
            else f"Infrastructure and public-policy discussion #{episode + 1}"
        )
        context = (
            "SYNTHETIC PUBLIC CALL: The fictional volunteer group openly asks its members to comment together at 08 minutes past the hour. This is transparent human coordination; automation and hostile intent must not be inferred."
            if activism
            else "SYNTHETIC SCENARIO: A fictional discussion with repeated joint account arrivals. The fixture does not establish who operates accounts or whether they are automated."
        )
        add_post(key, channel, hour, title, context)
        for index, account in enumerate(actors):
            if activism:
                text = f"Volunteer response {episode - 8}, participant {index + 1}: I saw the public call and am joining the requested discussion."
                urls = []
            else:
                text = (
                    f"Fictional claim example {episode + 1}.{index + 1}: "
                    + [
                        "partners cannot be relied on for support; this needs contextual review.",
                        "aid is being withheld deliberately, according to this unverified scenario claim.",
                        "the same policy concern deserves a response from officials.",
                        "official promises and visible outcomes do not match in this fictional example.",
                    ][index % 4]
                )
                urls = ["https://example.invalid/synthetic-claim"] if index < 3 else []
            add_message(
                key,
                channel,
                account,
                hour + timedelta(minutes=8, seconds=index * 7 + rng.randrange(4)),
                text,
                urls,
            )
        for account in rng.sample(background, 5):
            add_message(
                key,
                channel,
                account,
                hour + timedelta(seconds=rng.randrange(100, 2300)),
                "Independent fictional reply with its own timing.",
            )
        for other in range(4):
            other_key = f"syn-background-post-{episode + 1:02d}-{other + 1}"
            post_time = hour + timedelta(minutes=other + 1)
            add_post(
                other_key,
                channel,
                post_time,
                f"Unrelated local discussion {episode + 1}.{other + 1}",
            )
            for account in rng.sample(background, 13):
                add_message(
                    other_key,
                    channel,
                    account,
                    hour + timedelta(seconds=rng.randrange(300, 3300)),
                    "An independent synthetic comment on an unrelated discussion.",
                )

    # Organic outrage has one common burst and repeated surrounding activity.
    # The two different partitions below prevent any pair accumulating three
    # shared families solely from these examples.
    organic = [f"syn-organic-{i:02d}" for i in range(1, 21)]
    organic_time = start + timedelta(days=5, hours=1)
    organic_posts = ["syn-organic-burst"]
    add_post(
        organic_posts[0],
        "syn-channel-a",
        organic_time,
        "Breaking fictional infrastructure incident",
        "SYNTHETIC ORGANIC CONTROL: Many people react to the same fictional news event. Shared timing and anger alone do not establish recurrent coordination.",
    )
    for index, account in enumerate(organic):
        add_message(
            organic_posts[0],
            "syn-channel-a",
            account,
            organic_time + timedelta(seconds=30 + index * 4),
            f"Independent reaction {index + 1}: I am angry and worried about this fictional incident.",
        )
    for partition in ("row", "column"):
        group_count = 5 if partition == "row" else 4
        for group in range(group_count):
            key = f"syn-organic-context-{partition}-{group}"
            organic_posts.append(key)
            timestamp = organic_time + timedelta(hours=2 + group)
            add_post(
                key,
                "syn-channel-a",
                timestamp,
                f"Surrounding independent discussion {partition} {group}",
            )
            selected = [
                account
                for index, account in enumerate(organic)
                if (index // 4 if partition == "row" else index % 4) == group
            ]
            for index, account in enumerate(selected):
                add_message(
                    key,
                    "syn-channel-a",
                    account,
                    timestamp + timedelta(seconds=240 * index + 60),
                    "Independent criticism in another fictional discussion, with no repeated rapid joint arrival.",
                )

    critic_posts = []
    critic = "syn-prolific-critic-01"
    for episode in range(4):
        key = f"syn-critic-post-{episode + 1}"
        critic_posts.append(key)
        timestamp = start + timedelta(days=6, hours=episode)
        add_post(key, "syn-channel-b", timestamp, f"Fictional policy debate {episode + 1}")
        for comment in range(30):
            add_message(
                key,
                "syn-channel-b",
                critic,
                timestamp + timedelta(seconds=20 + comment * 17),
                f"Critical follow-up {comment + 1}: an active person can repeatedly question officials.",
            )

    dataset["comparisons"] = [
        {
            "case_id": "comparison-organic",
            "name": "Synthetic organic outrage and surrounding activity",
            "account_keys": organic,
            "post_keys": organic_posts,
            "description": "Synthetic control: one fast common reaction plus several surrounding discussions. Shared anger is not a scoring feature; recurrent pair evidence is insufficient.",
            "kind": "comparison",
        },
        {
            "case_id": "comparison-critic",
            "name": "Synthetic prolific individual critic",
            "account_keys": [critic],
            "post_keys": critic_posts,
            "description": "Synthetic control: 120 comments by one fictional critic. Volume alone cannot create a multi-account candidate.",
            "kind": "comparison",
        },
        {
            "case_id": "comparison-open-activism",
            "name": "Synthetic openly coordinated volunteers",
            "account_keys": volunteer_accounts,
            "post_keys": [f"syn-episode-{episode:02d}" for episode in range(10, 16)],
            "description": "Synthetic control: a public call explains genuine repeated coordination. The detector may surface it; hostile intent and automation remain unresolved.",
            "kind": "comparison",
        },
    ]
    dataset["manifest"].update(
        {
            "posts": len(dataset["posts"]),
            "messages": len(dataset["messages"]),
            "channels": len(dataset["channels"]),
        }
    )
    return dataset
