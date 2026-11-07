"""Application operations for the standalone Swarm CLI and dashboard.

Interactive reads use normalized SQL projections and bounded pages. Full corpus
materialization is an explicit analysis/snapshot operation, never a view cache.
This service owns specialist runs and review revisions in the Swarm database;
the Evidence Explorer adapter owns conversion into the shared evidence contract.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from collections import defaultdict
from collections.abc import Callable
from dataclasses import asdict
from functools import lru_cache
from pathlib import Path

from .models import (
    Case,
    CaseDetail,
    Concentration,
    Dataset,
    DatasetInfo,
    DatasetView,
    Page,
    PageInfo,
    Review,
    RunInfo,
    StressAvailability,
    WindowSensitivity,
)
from .persistence.paging import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, page_bounds
from .storage import Store

logger = logging.getLogger(__name__)
DECISIONS = ("Coordination supported", "Alternative explanation plausible", "Insufficient evidence")
ENDORSEMENTS = ("endorses", "rebuts", "quotes", "unclear")
ALIGNMENTS = ("supported", "mixed", "unclear", "not_established")
_INITIALIZATION_LOCK = threading.Lock()


def _collect_pages[T](read: Callable[..., Page[T]]) -> tuple[T, ...]:
    """Explicit complete-case export path; ordinary views never use this helper."""
    items = []
    offset = 0
    while True:
        page = read(limit=MAX_PAGE_SIZE, offset=offset)
        items.extend(page.items)
        if not page.has_more:
            return tuple(items)
        offset += len(page.items)


class Workbench:
    """Coordinate specialist repositories, detection, and saved review state.

    Construction opens and migrates the selected Swarm store. Analysis and sample
    creation are explicit methods, though the standalone web factory currently
    calls ``ensure_sample`` during startup. Shared-workspace decisions are not
    read from or synchronized into this store.
    """

    def __init__(self, db_path: str | Path | None = None):
        self.store = Store(db_path)
        # This cache holds only small robustness summaries keyed by immutable run ID.
        self._stress_test_cache = lru_cache(maxsize=12)(self._stress_test)

    def ensure_sample(self) -> dict:
        """Create and analyze a fictional sample when the store has no dataset.

        Existing datasets are inspected through metadata queries and are not
        automatically analyzed. A newly created sample is persisted together
        with its detector run before returning the overview.
        """
        with _INITIALIZATION_LOCK:
            dataset = self.store.datasets.info()
            created_sample = dataset is None
            if created_sample:
                from .sample_data import build_sample

                record = build_sample()
                self.store.put_dataset(record)
                dataset = self.store.datasets.info(record["dataset_id"])
            run = self.store.runs.info()
            if created_sample and (run is None or run.dataset_id != dataset.dataset_id):
                self.run_analysis()
        return self.overview()

    def run_analysis(self, config=None, dataset: dict | Dataset | None = None) -> dict:
        """Explicit full-corpus analysis; detector memory remains corpus-dependent."""
        from .detector import analyze

        if dataset is None:
            dataset = self.store.datasets.materialize()
        if dataset is None:
            raise ValueError("Import a dataset or initialize the synthetic demonstration first.")
        run = analyze(dataset.to_record() if isinstance(dataset, Dataset) else dataset, config)
        self.store.put_run(run)
        return run

    def _context(self, run_id: str | None = None) -> tuple[RunInfo, DatasetInfo]:
        run_id = run_id or self.store.active_run_id()
        if run_id is None:
            raise ValueError("No saved analysis run. Run the demo or analyze an imported dataset.")
        run = self.store.runs.info(run_id)
        if run is None:
            raise ValueError("Analysis run was not found.")
        dataset = self.store.datasets.info(run.dataset_id)
        if dataset is None:
            raise ValueError("The dataset for this analysis run was not found.")
        return run, dataset

    def snapshot(self, *, run_id: str | None = None) -> dict:
        """Explicitly materialize a complete source/result snapshot for integrations."""
        run, dataset = self._context(run_id)
        return {
            "dataset": self.store.datasets.materialize(dataset.dataset_id).to_record(),
            "run": self.store.runs.materialize(run.run_id).to_record(),
        }

    def overview(self, *, run_id: str | None = None) -> dict:
        if run_id is None and self.store.active_run_id() is None:
            from .config import DetectionConfig

            dataset = self.store.datasets.info()
            if dataset is None:
                raise ValueError("No imported dataset is available.")
            return {
                "dataset_id": dataset.dataset_id,
                "dataset_label": dataset.label,
                "synthetic": dataset.synthetic,
                "run_id": None,
                "ready": False,
                "workspace_status": "awaiting_analysis",
                **self.store.datasets.counts(dataset.dataset_id).model_dump(),
                "candidate_count": 0,
                "randomizations": 0,
                "config": DetectionConfig().as_dict(),
                "diagnostics": {},
                "manifest": dataset.manifest,
            }
        run, dataset = self._context(run_id)
        counts = self.store.datasets.counts(dataset.dataset_id)
        return {
            "dataset_id": dataset.dataset_id,
            "dataset_label": dataset.label,
            "synthetic": dataset.synthetic,
            "run_id": run.run_id,
            "ready": True,
            "workspace_status": "ready",
            **counts.model_dump(),
            "candidate_count": self.store.runs.candidate_count(run.run_id),
            "randomizations": run.config.randomizations,
            "config": asdict(run.config),
            "diagnostics": run.diagnostics,
            "manifest": dataset.manifest,
        }

    def readiness(self, *, run_id: str | None = None) -> dict:
        """Report whole-import coverage from SQL counts, not materialized records."""
        from .readiness import readiness_from_facts

        if run_id is not None or self.store.active_run_id() is not None:
            run, dataset = self._context(run_id)
        else:
            run = None
            dataset = self.store.datasets.info()
            if dataset is None:
                raise ValueError("No imported dataset is available.")
        return readiness_from_facts(
            dataset.to_record(),
            run.to_record() if run else None,
            self.store.readiness.facts(dataset.dataset_id, run.run_id if run else None),
        )

    def case_page(
        self,
        query: str = "",
        kind: str = "all",
        status: str = "all",
        *,
        limit: int = DEFAULT_PAGE_SIZE,
        offset: int = 0,
        run_id: str | None = None,
    ) -> dict:
        limit, offset = page_bounds(limit, offset)
        if run_id is None and self.store.active_run_id() is None:
            page = Page[Case](items=(), total=0, limit=limit, offset=offset)
        else:
            run, _ = self._context(run_id)
            page = self.store.cases.page(
                run.run_id, query, kind, status, limit=limit, offset=offset
            )
        return {"items": [case.to_record() for case in page.items], **page.metadata()}

    def list_cases(
        self,
        query: str = "",
        kind: str = "all",
        status: str = "all",
        *,
        limit: int = DEFAULT_PAGE_SIZE,
        offset: int = 0,
        run_id: str | None = None,
    ) -> list[dict]:
        """Compatibility list, now one bounded page; use case_page for total/has_more."""
        return self.case_page(query, kind, status, limit=limit, offset=offset, run_id=run_id)[
            "items"
        ]

    def get_review(
        self, case_id: str, *, run_id: str | None = None, revision: int | None = None
    ) -> dict | None:
        run, _ = self._context(run_id)
        review = (
            self.store.reviews.latest(run.run_id, case_id)
            if revision is None
            else self.store.reviews.revision(run.run_id, case_id, revision)
        )
        return review.to_record() if review else None

    def _case(self, run: RunInfo, case_id: str) -> Case:
        case = self.store.cases.get(run.run_id, case_id)
        if case is None:
            raise ValueError("Case was not found in the active run.")
        return case

    def _counterevidence(self, run: RunInfo, dataset: DatasetInfo, case: Case) -> tuple[str, ...]:
        notes = [
            "A candidate group does not establish bots, deception, foreign control, or an operator.",
            "Inspect parent posts for public calls to participate and shared breaking-news events.",
            "First arrivals describe joint entry into a thread and can miss coordination later in a discussion.",
            "The randomized reference preserves channel-hour activity and post time slots, but not every topic or reply preference.",
        ]
        if case.kind == "comparison":
            notes.insert(
                0, case.description or "Comparison activity without a retained candidate core."
            )
        if dataset.synthetic and self.store.cases.has_public_call(
            run.run_id, case.case_id, dataset.dataset_id
        ):
            notes.insert(
                0,
                "Synthetic context includes an explicit public volunteer call. Coordination may be real while hostile intent and automation remain unestablished.",
            )
        if run.diagnostics.get("limited_baseline_strata"):
            notes.append(
                "Some hourly strata have limited randomization; inspect baseline diagnostics before interpreting the comparison."
            )
        if run.diagnostics.get("discovery_omitted_post_count"):
            notes.append(
                "Dense posts were omitted from candidate discovery. Coverage is incomplete."
            )
        notes.extend(dataset.manifest.get("limitations", []))
        return tuple(notes)

    def _case_detail(
        self,
        run: RunInfo,
        dataset: DatasetInfo,
        case: Case,
        *,
        limit: int,
        message_offset: int,
        context_offset: int,
        post_offset: int,
        pair_offset: int,
        account_offset: int,
        channel_offset: int,
        event_offset: int,
        revision: int | None = None,
    ) -> CaseDetail:
        case_id = case.case_id
        args = (run.run_id, case_id, dataset.dataset_id)
        messages = self.store.cases.messages(*args, limit=limit, offset=message_offset)
        context = self.store.cases.messages(*args, context=True, limit=limit, offset=context_offset)
        posts = self.store.cases.posts(*args, limit=limit, offset=post_offset)
        accounts = self.store.cases.accounts(*args, limit=limit, offset=account_offset)
        channels = self.store.cases.channels(*args, limit=limit, offset=channel_offset)
        pairs = self.store.evidence.pairs(run.run_id, case_id, limit=limit, offset=pair_offset)
        events = self.store.evidence.case_events(
            run.run_id,
            case_id,
            pair_limit=limit,
            pair_offset=pair_offset,
            limit=limit,
            offset=event_offset,
        )
        by_pair = defaultdict(list)
        for item in events.items:
            by_pair[item.pair_key].append(item.event)
        retained = self.store.evidence.retained_count(run.run_id, case_id)
        sensitivity = self.store.evidence.sensitivity(
            run.run_id,
            case_id,
            dataset.manifest.get("timestamp_precision_seconds", 1),
            run.config.min_families,
        )
        pages = {
            "messages": messages,
            "context_messages": context,
            "posts": posts,
            "accounts": accounts,
            "channels": channels,
            "pairs": pairs,
            "events": events,
        }
        review = (
            self.store.reviews.latest(run.run_id, case_id)
            if revision is None
            else self.store.reviews.revision(run.run_id, case_id, revision)
        )
        case = case.model_copy(
            update={"review_status": review.decision if review else "Not reviewed"}
        )
        group_thread_comments, thread_comments = self.store.cases.thread_participation_counts(*args)
        return CaseDetail(
            case=case,
            run=run,
            dataset=DatasetView(**dataset.to_record(), channels=channels.items),
            accounts=accounts.items,
            posts=posts.items,
            messages=messages.items,
            context_messages=context.items,
            pairs=tuple(
                pair.model_copy(update={"events": tuple(by_pair[pair.pair_key])})
                for pair in pairs.items
            ),
            review=review,
            counterevidence=self._counterevidence(run, dataset, case),
            content_matches=self.store.evidence.content_matches(*args),
            concentration=Concentration(
                group_comments=group_thread_comments,
                thread_comments=thread_comments,
                share=group_thread_comments / thread_comments if thread_comments else 0,
            ),
            sensitivity=tuple(WindowSensitivity.model_validate(row) for row in sensitivity),
            stress_test=StressAvailability(available=bool(retained), retained_pair_count=retained),
            pagination={name: PageInfo(**page.metadata()) for name, page in pages.items()},
        )

    def case_detail(
        self,
        case_id: str,
        *,
        limit: int = DEFAULT_PAGE_SIZE,
        message_offset: int = 0,
        context_offset: int = 0,
        post_offset: int = 0,
        pair_offset: int = 0,
        account_offset: int = 0,
        channel_offset: int = 0,
        event_offset: int = 0,
        run_id: str | None = None,
        revision: int | None = None,
    ) -> dict:
        """Read at most ``limit`` rows per collection, including a global event page.

        Limit defaults to 200 and cannot exceed 500. Dataset/run records contain
        metadata only. Pagination totals describe the complete case, not the page.
        """
        run, dataset = self._context(run_id)
        detail = self._case_detail(
            run,
            dataset,
            self._case(run, case_id),
            limit=limit,
            message_offset=message_offset,
            context_offset=context_offset,
            post_offset=post_offset,
            pair_offset=pair_offset,
            account_offset=account_offset,
            channel_offset=channel_offset,
            event_offset=event_offset,
            revision=revision,
        )
        return detail.to_record()

    def pair_events(
        self,
        case_id: str,
        pair_key: str,
        *,
        limit: int = DEFAULT_PAGE_SIZE,
        offset: int = 0,
        run_id: str | None = None,
    ) -> dict:
        run, _ = self._context(run_id)
        self._case(run, case_id)
        if not self.store.evidence.pair_in_case(run.run_id, case_id, pair_key):
            raise ValueError("Pair was not found in the selected case.")
        page = self.store.evidence.events(run.run_id, pair_key, limit=limit, offset=offset)
        return {"items": [event.to_record() for event in page.items], **page.metadata()}

    def materialize_case(
        self, case_id: str, *, run_id: str | None = None, revision: int | None = None
    ) -> dict:
        """Explicit complete-case export; accumulate selected records, not the corpus."""
        run, dataset = self._context(run_id)
        case = self._case(run, case_id)
        detail = self._case_detail(
            run,
            dataset,
            case,
            limit=MAX_PAGE_SIZE,
            message_offset=0,
            context_offset=0,
            post_offset=0,
            pair_offset=0,
            account_offset=0,
            channel_offset=0,
            event_offset=0,
            revision=revision,
        )
        args = (run.run_id, case_id, dataset.dataset_id)
        messages = _collect_pages(lambda **page: self.store.cases.messages(*args, **page))
        context = _collect_pages(
            lambda **page: self.store.cases.messages(*args, context=True, **page)
        )
        posts = _collect_pages(lambda **page: self.store.cases.posts(*args, **page))
        accounts = _collect_pages(lambda **page: self.store.cases.accounts(*args, **page))
        channels = _collect_pages(lambda **page: self.store.cases.channels(*args, **page))
        pairs = _collect_pages(
            lambda **page: self.store.evidence.pairs(run.run_id, case_id, **page)
        )
        complete_pairs = []
        for pair in pairs:
            events = _collect_pages(
                lambda pair_key=pair.pair_key, **page: self.store.evidence.events(
                    run.run_id, pair_key, **page
                )
            )
            complete_pairs.append(pair.model_copy(update={"events": events}))
        return detail.model_copy(
            update={
                "messages": messages,
                "context_messages": context,
                "posts": posts,
                "accounts": accounts,
                "pairs": tuple(complete_pairs),
                "complete": True,
                "dataset": DatasetView(**dataset.to_record(), channels=channels),
                "pagination": {},
            }
        ).to_record()

    def save_review(
        self,
        case_id,
        reviewer,
        decision,
        reason,
        claim="",
        endorsement="unclear",
        alignment="unclear",
        narrative_notes="",
        evidence_refs=None,
        *,
        run_id: str | None = None,
        expected_revision: int | None = None,
    ) -> dict:
        run, dataset = self._context(run_id)
        self._case(run, case_id)
        if not isinstance(reviewer, str) or not reviewer.strip():
            raise ValueError("Add your reviewer name before saving.")
        if decision not in DECISIONS:
            raise ValueError("Choose one of the supported review decisions.")
        if not isinstance(reason, str) or len(reason.strip()) < 10:
            raise ValueError("Explain the review decision in at least ten characters.")
        if endorsement not in ENDORSEMENTS or alignment not in ALIGNMENTS:
            raise ValueError("Choose valid endorsement and narrative alignment values.")
        if evidence_refs is not None and (
            not isinstance(evidence_refs, (list, tuple, set))
            or not all(isinstance(ref, str) for ref in evidence_refs)
        ):
            raise ValueError("Narrative evidence references must be a list of message identifiers.")
        refs = tuple(sorted(set(evidence_refs or [])))
        self.store.cases.validate_evidence_refs(run.run_id, case_id, dataset.dataset_id, refs)
        review = Review(
            run_id=run.run_id,
            case_id=case_id,
            reviewer=reviewer.strip(),
            decision=decision,
            reason=reason.strip(),
            claim=claim.strip(),
            endorsement=endorsement,
            alignment=alignment,
            narrative_notes=narrative_notes.strip(),
            evidence_refs=refs,
            synthetic=dataset.synthetic,
        )
        return self.store.reviews.save(review, expected_revision=expected_revision).to_record()

    def search(self, query: str, limit: int = 30, *, run_id: str | None = None) -> list[dict]:
        _, dataset = self._context(run_id)
        return self.store.search(dataset.dataset_id, query, limit)

    def _stress_test(self, run_id: str, case_id: str) -> dict:
        from .detector import analyze

        run, dataset = self._context(run_id)
        case = self._case(run, case_id)
        if self.store.datasets.counts(dataset.dataset_id).message_count > 10_000:
            return {
                "available": False,
                "note": "Run sensitivity analysis offline for datasets above 10,000 comments.",
            }
        retained = self.store.evidence.retained_count(run_id, case_id)
        strongest = self.store.evidence.strongest_family(run_id, case_id)
        if strongest is None:
            return {"available": False, "note": "No retained group evidence to remove."}
        # Materialize only after the size guard, for an explicit detector rerun.
        filtered = self.store.datasets.materialize(dataset.dataset_id).to_record()
        excluded = {
            post["post_key"] for post in filtered["posts"] if post["post_family_key"] == strongest
        }
        filtered["posts"] = [post for post in filtered["posts"] if post["post_key"] not in excluded]
        filtered["messages"] = [
            message for message in filtered["messages"] if message["post_key"] not in excluded
        ]
        recomputed = analyze(filtered, asdict(run.config))
        core = set(case.account_keys)
        remaining = [
            pair
            for pair in recomputed["pairs"]
            if pair["retained"] and pair["account_a"] in core and pair["account_b"] in core
        ]
        remaining_core = sorted(
            {
                account
                for group in recomputed["groups"]
                for account in group["account_keys"]
                if account in core
            }
        )
        return {
            "available": True,
            "excluded_family": strongest,
            "original_pairs": retained,
            "remaining_pairs": len(remaining),
            "original_accounts": len(core),
            "remaining_accounts": len(remaining_core),
            "remaining_account_keys": remaining_core,
            "note": "Controls and groups were recomputed without the strongest post family. This checks fragility, not intent.",
        }

    def stress_test(self, case_id: str, *, run_id: str | None = None) -> dict:
        run, _ = self._context(run_id)
        # A fresh dict protects the cache; values are scalars plus one short key list.
        result = self._stress_test_cache(run.run_id, case_id)
        return {
            **result,
            **(
                {"remaining_account_keys": list(result["remaining_account_keys"])}
                if "remaining_account_keys" in result
                else {}
            ),
        }

    def export_case(
        self,
        case_id: str,
        format: str = "markdown",
        *,
        run_id: str | None = None,
        revision: int | None = None,
    ) -> str:
        from .export import export_case

        run, _ = self._context(run_id)
        if revision is None:
            review = self.store.reviews.latest(run.run_id, case_id)
            revision = review.revision if review else 0
        return export_case(
            self.materialize_case(case_id, run_id=run.run_id, revision=revision), format
        )

    def brief_preview(
        self, case_id: str, *, run_id: str | None = None, revision: int | None = None
    ) -> dict:
        """Preview one immutable saved revision, including the unreviewed revision zero."""
        run, dataset = self._context(run_id)
        self._case(run, case_id)
        review = (
            self.store.reviews.latest(run.run_id, case_id)
            if revision is None
            else self.store.reviews.revision(run.run_id, case_id, revision)
        )
        pinned = review.revision if review else 0
        return {
            "content": self.export_case(case_id, run_id=run.run_id, revision=pinned),
            "revision": pinned,
            "run_id": run.run_id,
            "case_id": case_id,
            "key": f"{run.run_id}::{case_id}",
            "synthetic": dataset.synthetic,
        }

    def health(self) -> dict:
        try:
            with self.store.connection() as conn:
                conn.execute("SELECT 1").fetchone()
            ready = self.store.active_run_id() is not None
            return {"status": "ok" if ready else "initializing", "ready": ready}
        except sqlite3.Error:
            logger.exception("Unable to read workbench health")
            return {"status": "unavailable", "ready": False}
