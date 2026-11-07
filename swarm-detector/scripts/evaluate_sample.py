#!/usr/bin/env python3
"""Evaluate synthetic fixture behaviour without opening the working database.

Run with: uv run python scripts/evaluate_sample.py
This is a technical sanity check, not human review or a real-world accuracy study.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.metadata
import io
import json
import os
import platform
import resource
import sqlite3
import sys
import tempfile
import time
import tracemalloc
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path

import swarm_detector
from swarm_detector.detector import analyze
from swarm_detector.sample_data import build_sample
from swarm_detector.service import Workbench

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TECHNICAL_REVIEWER = "AUTOMATED FIXTURE CHECK — NOT A HUMAN REVIEW"


def require(condition: object, message: str) -> None:
    """Keep evaluation checks active even if Python is run with optimization."""
    if not condition:
        raise AssertionError(message)


def verify_evidence_references(dataset: dict, run: dict) -> dict:
    """Resolve every pair event to its actual source records and account pair."""
    channels = {row["channel_key"] for row in dataset["channels"]}
    posts = {row["post_key"]: row for row in dataset["posts"]}
    messages = {row["message_key"]: row for row in dataset["messages"]}
    require(len(posts) == len(dataset["posts"]), "Post keys must be unique.")
    require(len(messages) == len(dataset["messages"]), "Message keys must be unique.")
    for post in posts.values():
        require(post["channel_key"] in channels, "A post's channel does not resolve.")
        require(bool(post.get("source_ref")), "A post lacks a source reference.")
    for message in messages.values():
        post = posts.get(message["post_key"])
        require(post is not None, "A message's parent post does not resolve.")
        require(message["channel_key"] == post["channel_key"], "Message channel mismatch.")
        require(bool(message.get("source_ref")), "A message lacks a source reference.")
    events = 0
    for pair in run["pairs"]:
        for event in pair["events"]:
            post = posts.get(event["post_key"])
            require(post is not None, "A pair event's post does not resolve.")
            require(event["family_key"] == post["post_family_key"], "Event family mismatch.")
            for side, account in (("a", "account_a"), ("b", "account_b")):
                message = messages.get(event[f"message_{side}"])
                require(message is not None, "A pair event's message does not resolve.")
                require(message["post_key"] == event["post_key"], "Event message/post mismatch.")
                require(message["account_key"] == pair[account], "Event account mismatch.")
                original = datetime.fromisoformat(message["timestamp_utc"].replace("Z", "+00:00"))
                observed = datetime.fromisoformat(event[f"timestamp_{side}"].replace("Z", "+00:00"))
                require(original == observed, "Event timestamp does not match its source.")
            require(
                event["delta_seconds"] <= run["config"]["window_seconds"],
                "An event falls outside the main timing window.",
            )
            events += 1
    return {
        "posts": len(posts),
        "messages": len(messages),
        "pairs": len(run["pairs"]),
        "events": events,
    }


def verify_thread_share(detail: dict) -> dict:
    """Keep a thread-share numerator inside the denominator's selected-post scope."""
    # Complete case records retain broader activity for community context. Only
    # declared evidence-thread keys define both sides of the participation share.
    post_keys = set(detail["case"]["post_keys"])
    require(
        post_keys <= {post["post_key"] for post in detail["posts"]},
        "A selected evidence thread lacks its parent post.",
    )
    core = set(detail["case"]["account_keys"])
    scoped_messages = [
        message
        for message in detail["messages"]
        if message["post_key"] in post_keys and message["account_key"] in core
    ]
    scoped_context = [
        message for message in detail["context_messages"] if message["post_key"] in post_keys
    ]
    context_refs = {message["message_key"] for message in scoped_context}
    require(
        {message["message_key"] for message in scoped_messages} <= context_refs,
        "In-thread core comments are absent from the selected thread context.",
    )
    concentration = detail["concentration"]
    numerator, denominator = len(scoped_messages), len(scoped_context)
    expected_share = numerator / denominator if denominator else 0
    require(
        concentration["group_comments"] == numerator
        and concentration["thread_comments"] == denominator
        and abs(concentration["share"] - expected_share) < 1e-12
        and 0 <= concentration["share"] <= 1,
        "Thread-share numerator, denominator, and selected-post scope disagree.",
    )
    return {
        "broader_core_activity_comments": len(detail["messages"]),
        "core_comments_in_selected_threads": numerator,
        "all_comments_in_selected_threads": denominator,
        "share": concentration["share"],
    }


def _source_hashes() -> dict[str, str]:
    package_root = Path(swarm_detector.__file__).resolve().parent
    paths = sorted([*package_root.rglob("*.py"), *package_root.rglob("*.css")])
    return {
        str(path.relative_to(package_root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in paths
    }


def _machine_scope() -> dict:
    versions = {}
    for name in ("swarm-detector", "dash", "dash-cytoscape", "plotly", "networkx", "pydantic"):
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = "not installed"
    return {
        "os": platform.platform(),
        "architecture": platform.machine(),
        "logical_cpus": os.cpu_count(),
        "python": platform.python_version(),
        "sqlite": sqlite3.sqlite_version,
        "packages": versions,
    }


def _all_cases(workbench: Workbench, run_id: str) -> list[dict]:
    """Walk the bounded queue explicitly instead of treating its first page as complete."""
    cases = []
    offset = 0
    while True:
        page = workbench.case_page(limit=100, offset=offset, run_id=run_id)
        cases.extend(page["items"])
        if not page["has_more"]:
            require(len(cases) == page["total"], "Case queue pagination lost records.")
            return cases
        require(bool(page["items"]), "Case queue pagination stopped making progress.")
        offset += len(page["items"])


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, delete=False
    ) as out:
        temporary = Path(out.name)
        try:
            out.write(content)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    temporary.replace(path)


def evaluate_sample(*, controls: int = 20) -> tuple[dict, str]:
    """Return a report and unreviewed packet using only fresh temporary storage."""
    if isinstance(controls, bool) or not isinstance(controls, int) or controls < 1:
        raise ValueError("controls must be a positive integer")
    started = time.perf_counter()
    dataset = build_sample()
    checks = []
    case_results = []
    packet_details = []
    timings = {}

    def check(name, feature, operation):
        tick = time.perf_counter()
        try:
            details = operation() or {}
            record = {"name": name, "feature": feature, "passed": True, "details": details}
        except Exception as exc:
            record = {
                "name": name,
                "feature": feature,
                "passed": False,
                "error": f"{type(exc).__name__}: {exc}",
            }
        record["seconds"] = round(time.perf_counter() - tick, 6)
        checks.append(record)

    def synthetic_only():
        require(dataset.get("synthetic") is True, "Only synthetic data is allowed.")
        require(dataset.get("manifest", {}).get("synthetic") is True, "Synthetic manifest missing.")
        for record in [*dataset["posts"], *dataset["messages"]]:
            require(record["source_ref"].startswith("synthetic://"), "Non-synthetic source found.")
        return {"real_records_accessed": 0, "working_database_opened": False}

    # Enforce scope before opening any database or running the detector.
    synthetic_only()
    with tempfile.TemporaryDirectory(prefix="swarm-sample-evaluation-") as temporary:
        db_path = Path(temporary) / "evaluation.db"
        workbench = Workbench(db_path)
        workbench.store.put_dataset(dataset)
        tracemalloc.start()
        tick = time.perf_counter()
        try:
            run = workbench.run_analysis({"randomizations": controls, "seed": 42})
            _, traced_peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        timings["analysis_and_sqlite_save_seconds_with_tracemalloc"] = round(
            time.perf_counter() - tick, 6
        )
        check("synthetic_input_scope", "all", synthetic_only)
        check(
            "source_and_pair_references_resolve",
            "verify_evidence",
            lambda: verify_evidence_references(dataset, run),
        )

        def recovered_cohorts():
            groups = [set(group["account_keys"]) for group in run["groups"]]
            repeated = {
                m["account_key"]
                for m in dataset["messages"]
                if (m.get("account_key") or "").startswith("syn-repeat-")
            }
            volunteers = next(
                c for c in dataset["comparisons"] if c["case_id"] == "comparison-open-activism"
            )
            require(len(repeated) >= 3, "Repeated-arrival fixture is absent.")
            require(
                any(repeated <= members for members in groups),
                "Repeated fixture cohort was not recovered.",
            )
            require(
                any(set(volunteers["account_keys"]) <= members for members in groups),
                "Publicly coordinated fixture cohort was not recovered.",
            )
            require(
                all(
                    group["family_count"] >= run["config"]["min_families"]
                    for group in run["groups"]
                ),
                "A group lacks repeated-family support.",
            )
            return {
                "candidate_groups": len(groups),
                "repeated_cohort_members": len(repeated),
                "public_volunteer_members": len(volunteers["account_keys"]),
                "interpretation": "Technical fixture recovery, not real-world recall.",
            }

        check("repeated_and_public_cohorts_recovered", "find_candidates", recovered_cohorts)

        def organic_controls():
            members = {account for group in run["groups"] for account in group["account_keys"]}
            results = []
            for case_id in ("comparison-organic", "comparison-critic"):
                comparison = next(c for c in dataset["comparisons"] if c["case_id"] == case_id)
                overlap = sorted(members & set(comparison["account_keys"]))
                require(not overlap, f"{case_id} unexpectedly entered a candidate core.")
                results.append({"case_id": case_id, "accounts_in_candidate_core": len(overlap)})
            return {
                "fixture_outcomes": results,
                "interpretation": "Two designed counterexamples, not a population false-positive estimate.",
            }

        check(
            "organic_outrage_and_prolific_critic_not_candidates",
            "find_candidates",
            organic_controls,
        )

        def transparent_coordination():
            detail = workbench.materialize_case(
                "comparison-open-activism", run_id=run["run_id"], revision=0
            )
            retained = sum(pair["retained"] for pair in detail["pairs"])
            require(retained > 0, "The volunteer fixture should have coordination evidence.")
            require(
                any("public volunteer call" in note.lower() for note in detail["counterevidence"]),
                "Public coordination context is not exposed.",
            )
            require(
                any("PUBLIC CALL" in post["text_original"] for post in detail["posts"]),
                "Public source context is absent.",
            )
            return {
                "retained_pairs": retained,
                "fixture_context": "An explicit public call explains transparent human coordination.",
                "automation": "not_established",
                "hostile_intent": "not_established",
                "operator_attribution": "not_made",
            }

        check(
            "public_coordination_is_not_a_hostile_intent_finding",
            "verify_evidence",
            transparent_coordination,
        )

        def repeatability():
            repeated = analyze(dataset, run["config"])
            require(
                repeated["run_id"] == run["run_id"], "Same input/settings changed run identity."
            )
            require(
                repeated["pairs"] == run["pairs"] and repeated["groups"] == run["groups"],
                "Same input/settings changed behavioural results.",
            )
            return {"same_run_id": True, "same_pairs_and_groups": True}

        check("same_input_and_seed_repeat", "find_candidates", repeatability)

        def wording_invariance():
            changed = deepcopy(dataset)
            for message in changed["messages"]:
                message["text_original"] = (
                    "[Synthetic invariance probe] Opposite opinion, quotation, or criticism."
                )
                message["urls"] = ["https://example.invalid/different-invariance-probe"]
            for post in changed["posts"]:
                post["title"] = "[Synthetic invariance probe] Different wording"
                post["text_original"] = "[Synthetic invariance probe] Different context wording"
            for channel in changed["channels"]:
                channel["category"] = "Changed fictional category for invariance check"
                channel["rationale"] = "Not a reviewed real-world classification."
            changed["narrative_labels"] = ["Changed fictional narrative label"]
            result = analyze(changed, run["config"])
            require(
                result["pairs"] == run["pairs"] and result["groups"] == run["groups"],
                "Wording or labels changed behavioural results.",
            )
            require(
                result["input_fingerprint"] == run["input_fingerprint"],
                "Structural fingerprint changed.",
            )
            require(
                result["source_fingerprint"] != run["source_fingerprint"],
                "Changed source text was not reflected in provenance.",
            )
            return {
                "same_pairs_and_groups": True,
                "same_structural_fingerprint": True,
                "changed_source_fingerprint": True,
            }

        check(
            "wording_links_and_labels_do_not_change_detection",
            "find_candidates",
            wording_invariance,
        )

        def case_scope():
            event_count = 0
            for case in _all_cases(workbench, run["run_id"]):
                bounded = workbench.case_detail(
                    case["case_id"], limit=25, run_id=run["run_id"], revision=0
                )
                detail = workbench.materialize_case(
                    case["case_id"], run_id=run["run_id"], revision=0
                )
                require(detail["run"]["run_id"] == run["run_id"], "Case opened another run.")
                require(detail["messages"] and detail["posts"], "A case lacks source material.")
                require(detail.get("complete") is True, "Explicit case export was incomplete.")
                for collection in ("messages", "context_messages", "posts", "pairs", "accounts"):
                    require(
                        len(bounded[collection]) <= 25, "A bounded case view exceeded its limit."
                    )
                    require(
                        bounded["pagination"][collection]["total"] == len(detail[collection]),
                        "Bounded-view total and complete-case count disagree.",
                    )
                refs = {message["message_key"] for message in detail["messages"]}
                thread_share = verify_thread_share(detail)
                for pair in detail["pairs"]:
                    for event in pair["events"]:
                        require(
                            event["message_a"] in refs and event["message_b"] in refs,
                            "Case evidence points outside its source records.",
                        )
                        event_count += 1
                require(
                    detail["review"] is None, "A fresh packet unexpectedly contains a human review."
                )
                row = {
                    "case_id": case["case_id"],
                    "name": case["name"],
                    "kind": case["kind"],
                    "accounts": len(case["account_keys"]),
                    "post_families": case["family_count"],
                    "retained_pairs": sum(pair["retained"] for pair in detail["pairs"]),
                    "thread_share": thread_share,
                    "review_status": "not_human_reviewed",
                }
                case_results.append(row)
                packet_details.append(detail)
            return {
                "cases_checked": len(case_results),
                "case_pair_events_checked": event_count,
                "bounded_view_limit_checked": 25,
                "complete_case_export_checked": True,
            }

        check("all_case_views_resolve_their_evidence", "verify_evidence", case_scope)

        def review_and_exports():
            candidate = workbench.list_cases(kind="candidate", run_id=run["run_id"])[0]
            case_id = candidate["case_id"]
            detail = workbench.case_detail(case_id, run_id=run["run_id"], revision=0)
            source_ref = detail["messages"][0]["message_key"]
            unreviewed_preview = workbench.brief_preview(case_id, run_id=run["run_id"], revision=0)
            require(unreviewed_preview["revision"] == 0, "Unreviewed preview was not pinned.")
            arguments = {
                "reviewer": TECHNICAL_REVIEWER,
                "decision": "Insufficient evidence",
                "claim": "Synthetic field-roundtrip probe; no narrative judgment has been made.",
                "endorsement": "unclear",
                "alignment": "not_established",
                "narrative_notes": "Automated serialization check only. A qualified person has not reviewed this claim or its context.",
                "evidence_refs": [source_ref],
                "run_id": run["run_id"],
            }
            first = workbench.save_review(
                case_id,
                reason="Automated persistence probe only; human contextual review is still required.",
                expected_revision=0,
                **arguments,
            )
            first_preview = workbench.brief_preview(
                case_id, run_id=run["run_id"], revision=first["revision"]
            )
            latest = workbench.save_review(
                case_id,
                reason="Second automated revision checks persistence; it remains outside human review.",
                expected_revision=first["revision"],
                **arguments,
            )
            reopened = Workbench(db_path)
            reopened.ensure_sample()
            stored = reopened.get_review(case_id, run_id=run["run_id"], revision=latest["revision"])
            require(
                stored["revision"] == latest["revision"] > first["revision"],
                "Saved review revisions did not survive reopen.",
            )
            require(stored["reviewer"] == TECHNICAL_REVIEWER, "Technical reviewer label changed.")
            require(
                stored["run_id"] == run["run_id"] and stored["evidence_refs"] == [source_ref],
                "Review source/run identity changed.",
            )
            history = reopened.store.reviews.history(run["run_id"], case_id)
            require(len(history) == 2, "Append-only technical revision history was not preserved.")
            try:
                reopened.save_review(
                    case_id,
                    reason="This deliberately stale technical write must be rejected.",
                    expected_revision=first["revision"],
                    **arguments,
                )
            except ValueError:
                pass
            else:
                raise AssertionError("A stale review revision silently overwrote the saved state.")
            switched = reopened.run_analysis({**run["config"], "seed": 43})
            require(
                switched["run_id"] != run["run_id"], "The run-switch probe did not switch runs."
            )
            require(
                reopened.brief_preview(case_id, run_id=run["run_id"], revision=0)["content"]
                == unreviewed_preview["content"],
                "Revision zero changed after a later review or active-run switch.",
            )
            require(
                reopened.export_case(
                    case_id, "markdown", run_id=run["run_id"], revision=first["revision"]
                )
                == first_preview["content"],
                "An earlier saved preview changed after a later review or active-run switch.",
            )
            latest_preview = reopened.brief_preview(
                case_id, run_id=run["run_id"], revision=latest["revision"]
            )
            exported = json.loads(
                reopened.export_case(
                    case_id, "json", run_id=run["run_id"], revision=latest["revision"]
                )
            )
            require(exported["synthetic"] is True, "JSON export lost its synthetic label.")
            require(
                exported["review"]["revision"] == latest["revision"],
                "JSON export does not contain the saved revision.",
            )
            exported_refs = {m["message_key"] for m in exported["messages"]}
            require(
                len(exported["messages"]) == detail["pagination"]["messages"]["total"],
                "JSON export was truncated to a bounded case page.",
            )
            require(
                set(exported["review"]["evidence_refs"]) <= exported_refs,
                "Reviewed sources do not resolve in JSON export.",
            )
            markdown = reopened.export_case(
                case_id, "markdown", run_id=run["run_id"], revision=latest["revision"]
            )
            require(
                markdown == latest_preview["content"], "Saved preview and Markdown export differ."
            )
            require(
                "SYNTHETIC DEMONSTRATION" in markdown and TECHNICAL_REVIEWER in markdown,
                "Markdown export lacks synthetic/technical labels.",
            )
            require(
                str(latest["revision"]) in markdown and source_ref in markdown,
                "Markdown export lacks review/source evidence.",
            )
            csv_rows = list(
                csv.DictReader(
                    io.StringIO(
                        reopened.export_case(
                            case_id, "csv", run_id=run["run_id"], revision=latest["revision"]
                        )
                    )
                )
            )
            require(
                len(csv_rows) == len(exported["messages"]),
                "CSV and JSON case message counts disagree.",
            )
            require(
                all(
                    row["synthetic"] == "True" and row["run_id"] == run["run_id"]
                    for row in csv_rows
                ),
                "CSV provenance mismatch.",
            )
            require(
                {row["message_key"] for row in csv_rows} == exported_refs,
                "CSV and JSON source references disagree.",
            )
            return {
                "technical_reviewer": TECHNICAL_REVIEWER,
                "technical_revisions_written": len(history),
                "saved_revision": latest["revision"],
                "stale_write_rejected": True,
                "unreviewed_revision_zero_preserved": True,
                "earlier_saved_preview_preserved": True,
                "saved_preview_matches_export": True,
                "pinned_export_survives_active_run_switch": True,
                "formats_checked": ["markdown", "json", "csv"],
                "human_reviews_completed": 0,
            }

        check(
            "review_reopen_preview_and_exports_preserve_pinned_saved_evidence",
            "review_and_brief",
            review_and_exports,
        )

        def backup_roundtrip():
            expected_active_run = workbench.store.active_run_id()
            backup = workbench.store.backup(Path(temporary) / "backup.db")
            with sqlite3.connect(backup) as connection:
                require(
                    connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok",
                    "Backup integrity check failed.",
                )
                messages = connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
                require(messages == len(dataset["messages"]), "Backup message count mismatch.")
            restored = Workbench(backup)
            require(
                restored.overview()["run_id"] == expected_active_run,
                "Backup does not reopen the same active run.",
            )
            require(
                restored.overview(run_id=run["run_id"])["run_id"] == run["run_id"],
                "Backup lost the earlier pinned run.",
            )
            require(restored.health()["ready"] is True, "Restored backup is not ready.")
            return {
                "sqlite_integrity": "ok",
                "messages": messages,
                "same_saved_run": True,
                "scope": "Temporary local fixture DB; no container restart is tested here.",
            }

        check("sqlite_backup_reopens", "operations", backup_roundtrip)

    timings["whole_evaluation_seconds"] = round(time.perf_counter() - started, 6)
    peak_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    report = {
        "schema_version": 1,
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "scope": "Technical checks on deterministic synthetic fixtures only; no real Telegram data or human judgments.",
        "all_checks_passed": bool(checks) and all(row["passed"] for row in checks),
        "check_counts": {
            "passed": sum(row["passed"] for row in checks),
            "failed": sum(not row["passed"] for row in checks),
            "total": len(checks),
        },
        "dataset": {
            "dataset_id": dataset["dataset_id"],
            "label": dataset["label"],
            "synthetic": True,
            "posts": len(dataset["posts"]),
            "messages": len(dataset["messages"]),
            "channels": len(dataset["channels"]),
            "accounts": len(
                {m["account_key"] for m in dataset["messages"] if m.get("account_key")}
            ),
            "sampling": "Every case generated by this fixture; not a sample of real Telegram activity.",
        },
        "run": {
            key: run[key]
            for key in (
                "run_id",
                "config",
                "input_fingerprint",
                "source_fingerprint",
                "software_versions",
                "diagnostics",
            )
        },
        "machine": _machine_scope(),
        "measurements": {
            **timings,
            "analysis_python_traced_peak_bytes": traced_peak,
            "process_peak_rss_bytes": int(
                peak_rss if sys.platform == "darwin" else peak_rss * 1024
            ),
            "scope_note": "Analysis timing includes SQLite save and tracemalloc overhead. RSS is this entire Python process's lifetime peak, not detector-only memory. No browser, Docker, or real-data benchmark is measured by this script.",
        },
        "human_review": {
            "status": "not_conducted",
            "completed_reviews": 0,
            "independent_reviewers": [],
            "narrative_validation": "not_conducted",
            "technical_writer": TECHNICAL_REVIEWER,
        },
        "real_world_accuracy": {
            "measured": False,
            "reason": "Synthetic fixture outcomes cannot establish real-world precision, recall, bot accuracy, hostile intent, or foreign control.",
        },
        "browser_interactions": {
            "tested_by_this_script": False,
            "reason": "This harness exercises service and export contracts; record browser and container checks separately.",
        },
        "real_archive_accessed": False,
        "working_database_opened": False,
        "checks": checks,
        "cases": case_results,
        "source_sha256": _source_hashes(),
        "evaluator_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }
    return report, _reviewer_packet(report, packet_details)


def _reviewer_packet(report: dict, details: list[dict]) -> str:
    lines = [
        "# Synthetic fixture reviewer packet",
        "",
        "**NOT HUMAN-REVIEWED. All accounts, discussions, statements, and source references are fictional.**",
        "",
        "This packet prepares a review exercise. Automated fixture outcomes are recorded in `sample-results.json`; they are not independent judgments. No real-world accuracy or attribution is established.",
        "",
        f"Dataset: `{report['dataset']['dataset_id']}`",
        f"Run: `{report['run']['run_id']}`",
        f"Scope: {report['dataset']['posts']} posts, {report['dataset']['messages']} comments, {report['dataset']['channels']} fictional channels.",
        "",
        "Reproduce with `uv run python scripts/evaluate_sample.py`. Open the synthetic workbench for full evidence; every source can also be regenerated with `build_sample()`. Synthetic source references are identifiers, not live Telegram links.",
        "",
        "First review repeated behavioural evidence and possible organic explanations. Then inspect original context and decide whether a claim is endorsed, quoted, rebutted, or unclear. Coordination does not establish hostile intent or automation. The public-volunteer fixture deliberately tests that distinction.",
        "",
        "Completed qualified reviews: **0**. Independent reviewers: **none recorded**. Narrative judgments: **not conducted**. The script's temporary technical write/reopen probe is not a reviewer opinion and is never saved into the working application database.",
    ]
    for detail in details:
        case = detail["case"]
        retained = [pair for pair in detail["pairs"] if pair["retained"]]
        lines += [
            "",
            f"## {case['case_id']} — {case['name']}",
            "",
            f"View: {case['kind']}; accounts: {len(case['account_keys'])}; distinct post families: {case['family_count']}; retained pair links: {len(retained)}.",
            "",
            "Accounts: " + ", ".join(case["account_keys"]),
            "",
            "### Evidence to inspect",
            "",
        ]
        for pair in retained[:2]:
            lines.append(
                f"- `{pair['account_a']}` / `{pair['account_b']}`: {pair['observed']} observed families; shuffled mean {pair['null_mean']:.2f}; shuffled 95th percentile {pair['null_p95']:.2f}. These are descriptive references, not bot probabilities."
            )
            for event in pair["events"][:2]:
                lines.append(
                    f"  - Post `{event['post_key']}`; comments `{event['message_a']}` and `{event['message_b']}`; gap {event['delta_seconds']:.0f} seconds."
                )
        if not retained:
            lines.append(
                "No retained pair links in this comparison. Inspect its activity directly; absence of a retained link is not proof that an account is genuine."
            )
        lines += ["", "### Original fictional context", ""]
        for post in detail["posts"][:2]:
            lines += [
                f"Post `{post['post_key']}` · `{post['source_ref']}`",
                "",
                *["> " + line for line in post["text_original"].splitlines()],
                "",
            ]
        for message in detail["messages"][:2]:
            lines += [
                f"Comment `{message['message_key']}` · `{message['timestamp_utc']}` · `{message['source_ref']}`",
                "",
                *["> " + line for line in message["text_original"].splitlines()],
                "",
            ]
        lines += [
            "### Counterevidence and limits",
            "",
            *["- " + note for note in detail["counterevidence"]],
            "",
            "### Human review — incomplete",
            "",
            "- Reviewer and review time: not recorded.",
            "- Behavioural decision and reasoning: not reviewed.",
            "- Alternative explanation: not reviewed.",
            "- Claim and selected source-comment IDs, if any: not reviewed.",
            "- Endorsement and narrative alignment, with contextual rationale: not reviewed.",
            "- Disagreement or additional evidence needed: not reviewed.",
        ]
    lines += [
        "",
        "## Interpretation limits",
        "",
        "These designed fixtures exercise the pipeline and review safeguards. They do not represent the diversity, prevalence, identity quality, language, or adaptive behaviour of actual Telegram activity. A qualified, independent source-language review and a defensible real-data sample remain outstanding.",
        "",
    ]
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=PROJECT_ROOT / "evaluation" / "review")
    parser.add_argument(
        "--controls",
        type=int,
        default=20,
        help="Fixed-seed synthetic randomized controls (default: 20).",
    )
    args = parser.parse_args(argv)
    try:
        report, packet = evaluate_sample(controls=args.controls)
    except Exception as exc:
        report = {
            "schema_version": 1,
            "generated_at_utc": datetime.now(UTC).isoformat(),
            "all_checks_passed": False,
            "scope": "Synthetic fixture evaluation failed before completion.",
            "error": f"{type(exc).__name__}: {exc}",
            "human_review": {"status": "not_conducted", "completed_reviews": 0},
            "real_world_accuracy": {"measured": False},
        }
        packet = "# Synthetic reviewer packet unavailable\n\nThe technical evaluation failed. No human review or real-world accuracy result is available. See `sample-results.json`.\n"
    output_dir = args.output_dir.expanduser().resolve()
    _atomic_write(
        output_dir / "sample-results.json", json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    )
    _atomic_write(output_dir / "reviewer-packet.md", packet)
    print(
        json.dumps(
            {
                "all_checks_passed": report["all_checks_passed"],
                "check_counts": report.get("check_counts"),
                "human_reviews_completed": 0,
                "real_world_accuracy_measured": False,
                "output_dir": str(output_dir),
            },
            indent=2,
        )
    )
    return 0 if report["all_checks_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
