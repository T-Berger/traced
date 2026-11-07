"""Build normalized read tables inside the dataset/run write transaction."""

from __future__ import annotations

import sqlite3

from ..models import AnalysisRun, Case, Dataset
from ..serialization import canonical_json


def project_dataset(conn: sqlite3.Connection, dataset: Dataset) -> None:
    info = dataset.info()
    conn.execute(
        "INSERT INTO dataset_info VALUES (?,?,?,?,?,?,?)",
        (
            dataset.dataset_id,
            canonical_json(info.to_record()),
            len(dataset.channels),
            len(dataset.posts),
            len(dataset.messages),
            len({message.account_key for message in dataset.messages if message.account_key}),
            int(dataset.synthetic),
        ),
    )
    conn.executemany(
        "INSERT INTO channels VALUES (?,?,?,?)",
        (
            (dataset.dataset_id, channel.channel_key, ordinal, canonical_json(channel.to_record()))
            for ordinal, channel in enumerate(dataset.channels)
        ),
    )
    conn.executemany(
        "INSERT INTO posts VALUES (?,?,?,?,?,?,?)",
        (
            (
                dataset.dataset_id,
                post.post_key,
                post.channel_key,
                post.post_family_key,
                post.timestamp_utc,
                ordinal,
                canonical_json(post.to_record()),
            )
            for ordinal, post in enumerate(dataset.posts)
        ),
    )
    conn.executemany(
        "INSERT INTO dataset_comparisons VALUES (?,?,?,?)",
        (
            (dataset.dataset_id, case.case_id, ordinal, canonical_json(case.to_record()))
            for ordinal, case in enumerate(dataset.comparisons)
        ),
    )
    conn.executemany(
        "INSERT INTO message_content VALUES (?,?,?,?)",
        (
            (
                dataset.dataset_id,
                message.message_key,
                ordinal,
                " ".join(message.text_original.casefold().split()),
            )
            for ordinal, message in enumerate(dataset.messages)
        ),
    )
    conn.executemany(
        "INSERT INTO message_urls VALUES (?,?,?)",
        (
            (dataset.dataset_id, message.message_key, url)
            for message in dataset.messages
            for url in set(message.urls)
        ),
    )


def _project_case(conn: sqlite3.Connection, run: AnalysisRun, case: Case, ordinal: int) -> None:
    values = case.to_record()
    values.setdefault("kind", "comparison")
    values.setdefault("peripheral_keys", [])
    values.setdefault("pair_keys", [])
    values.setdefault("rank_score", 0)
    conn.execute(
        "INSERT INTO cases VALUES (?,?,?,?,?,?,?)",
        (
            run.run_id,
            case.case_id,
            ordinal,
            case.kind,
            case.name,
            " ".join([case.case_id, case.name, case.description, *case.account_keys]).casefold(),
            canonical_json(values),
        ),
    )
    conn.executemany(
        "INSERT OR IGNORE INTO case_accounts VALUES (?,?,?,?)",
        (
            (run.run_id, case.case_id, account, core)
            for core, accounts in ((1, case.account_keys), (0, case.peripheral_keys))
            for account in accounts
        ),
    )
    conn.executemany(
        "INSERT OR IGNORE INTO case_posts VALUES (?,?,?,?)",
        ((run.run_id, case.case_id, key, 1) for key in case.post_keys),
    )
    conn.execute(
        """INSERT OR IGNORE INTO case_posts
        SELECT DISTINCT a.run_id, a.case_id, m.post_key, 0
        FROM case_accounts a JOIN messages m ON m.account_key=a.account_key
        WHERE a.run_id=? AND a.case_id=? AND a.is_core=1 AND m.dataset_id=?""",
        (run.run_id, case.case_id, run.dataset_id),
    )
    if "family_count" not in values:
        values["family_count"] = conn.execute(
            """SELECT COUNT(DISTINCT p.post_family_key) FROM case_posts c
            JOIN posts p ON p.post_key=c.post_key AND p.dataset_id=?
            WHERE c.run_id=? AND c.case_id=? AND c.declared=1""",
            (run.dataset_id, run.run_id, case.case_id),
        ).fetchone()[0]
        conn.execute(
            "UPDATE cases SET payload=? WHERE run_id=? AND case_id=?",
            (canonical_json(values), run.run_id, case.case_id),
        )


def project_run(conn: sqlite3.Connection, run: AnalysisRun) -> None:
    conn.execute(
        "INSERT INTO run_info VALUES (?,?,?,?)",
        (run.run_id, run.dataset_id, canonical_json(run.info().to_record()), len(run.groups)),
    )
    conn.executemany(
        "INSERT INTO pairs VALUES (?,?,?,?,?,?,?)",
        (
            (
                run.run_id,
                pair.pair_key,
                pair.account_a,
                pair.account_b,
                ordinal,
                int(pair.retained),
                canonical_json(
                    pair.model_dump(mode="json", exclude={"events"}, exclude_unset=True)
                ),
            )
            for ordinal, pair in enumerate(run.pairs)
        ),
    )
    conn.executemany(
        "INSERT INTO pair_events VALUES (?,?,?,?)",
        (
            (run.run_id, pair.pair_key, ordinal, canonical_json(event.to_record()))
            for pair in run.pairs
            for ordinal, event in enumerate(pair.events)
        ),
    )
    for ordinal, case in enumerate(run.groups):
        _project_case(conn, run, case, ordinal)
    comparisons = conn.execute(
        "SELECT payload FROM dataset_comparisons WHERE dataset_id=? ORDER BY ordinal",
        (run.dataset_id,),
    )
    for ordinal, row in enumerate(comparisons, len(run.groups)):
        _project_case(conn, run, Case.model_validate_json(row[0]), ordinal)
