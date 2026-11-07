"""Sequential collection and assessment, with a persisted freeze before inference."""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import UTC, datetime
from threading import Event, Thread
from typing import TYPE_CHECKING

from .contracts import (
    ClaimCheckJob,
    ClaimCheckResult,
    EvidenceSnapshot,
    Issue,
    ProposedAssessment,
    SearchBatch,
    SearchHit,
)
from .storage import StaleClaimCheck

if TYPE_CHECKING:
    from .service import ClaimCheckService

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _balanced_hits(seeds: list[SearchHit], searches: list[SearchBatch]) -> list[SearchHit]:
    """Give every query an opportunity before selecting another lead from one query."""
    from ..ingestion.models import CollectionError
    from ..ingestion.url_policy import normalize_url

    groups = [iter(seeds), *[iter(batch.hits) for batch in searches]]
    seen: set[str] = set()
    hits: list[SearchHit] = []
    while groups:
        remaining = []
        for group in groups:
            for hit in group:
                try:
                    key = normalize_url(hit.url)
                except CollectionError:
                    key = hit.url
                if key in seen:
                    continue
                seen.add(key)
                hits.append(hit)
                remaining.append(group)
                break
        groups = remaining
    return hits


def _collect(service: ClaimCheckService, job: ClaimCheckJob) -> EvidenceSnapshot:
    from .assessor import select_fragments
    from .collection import collect_evidence
    from .discovery import default_search_provider

    policy = job.payload.policy
    claim = job.payload.claim
    discovery = service.discovery or default_search_provider()
    queries = policy.queries or [claim[:600], f"{claim[:600]} correction evidence"]
    searches: list[SearchBatch] = []
    issues: list[Issue] = []
    seeds = [
        SearchHit(url=url, title="Analyst-supplied source", provider="analyst", rank=index + 1)
        for index, url in enumerate(policy.seed_urls)
    ]
    service.storage.stage(job, "discovering_sources")
    try:
        for query in queries:
            service.storage.validate_context(job)
            try:
                batch = SearchBatch.model_validate(
                    discovery.search(query, limit=policy.results_per_query)
                )
            except Exception:
                # Provider exception text can contain request headers or source content.
                batch = SearchBatch(
                    query=query,
                    provider=type(discovery).__name__,
                    searched_at=_now(),
                    issues=[
                        Issue(
                            code="search_failed",
                            message="The search provider could not complete this query.",
                            query=query,
                        )
                    ],
                )
            searches.append(batch)
            issues.extend(batch.issues)
    finally:
        if service.discovery is None and hasattr(discovery, "close"):
            discovery.close()
    service.storage.validate_context(job)
    service.storage.stage(job, "collecting_sources")
    hits = _balanced_hits(seeds, searches)
    collection = collect_evidence(hits, collector=service.collector, limit=policy.max_sources)
    issues.extend(collection.issues)
    uncollected_count = len(hits) - len(collection.attempted_urls)
    if uncollected_count > 0:
        issues.append(
            Issue(
                code="collection_budget",
                message=f"{uncollected_count} discovered unique URLs were not fetched because "
                f"the collection limit is {policy.max_sources}. Seeds and search queries "
                "receive alternating collection slots.",
            )
        )
    service.storage.stage(job, "preparing_evidence")
    return EvidenceSnapshot(
        records=collection.records,
        fragments=select_fragments(claim, collection.records),
        searches=searches,
        issues=issues,
        attempted_urls=collection.attempted_urls,
        created_at=_now(),
        limitations=[
            "Search is bounded and may omit relevant evidence; it does not establish completeness.",
            "Publisher names and distinct URLs do not establish independent corroboration.",
            "Only saved text excerpts are supplied to the local assessment model.",
            "Text checking does not authenticate images, identify bots, "
            "or establish deceptive intent.",
        ],
    )


def _assess(
    service: ClaimCheckService, job: ClaimCheckJob, snapshot: EvidenceSnapshot
) -> ProposedAssessment:
    from .assessor import OllamaClaimAssessor, validate_assessment

    if not snapshot.fragments:
        assessment = ProposedAssessment(
            status="insufficient_evidence",
            explanation="No usable external source passages were collected for this claim.",
            unresolved_questions=["Which accessible primary sources can establish this claim?"],
            provider="deterministic",
            prompt_version="empty-evidence-v1",
            metadata={"model_called": False},
        )
    else:
        assessor = service.assessor or OllamaClaimAssessor(
            replace(service.settings, chat_model=job.payload.requested_model)
        )
        assessment = ProposedAssessment.model_validate(assessor.assess(job.payload.claim, snapshot))
    validate_assessment(assessment, snapshot)
    return assessment


def run_next(service: ClaimCheckService) -> bool:
    """Process one durable job. The workspace worker supplies the process lock."""
    from .bundles import build_result_bundle

    job = service.storage.claim()
    if job is None:
        return False
    stop = Event()

    def heartbeat() -> None:
        while not stop.wait(max(0.05, min(30, service.storage.LEASE_SECONDS / 3))):
            try:
                if not service.storage.heartbeat(job):
                    return
            except Exception:
                logger.warning("Could not renew claim-check lease for %s", job.id)

    thread = Thread(target=heartbeat, name="claim-check-lease", daemon=True)
    thread.start()
    try:
        service.storage.validate_context(job)
        snapshot = job.snapshot or _collect(service, job)
        service.storage.freeze(job, snapshot)
        service.storage.validate_context(job)
        service.storage.stage(job, "assessing_claim")
        assessment = _assess(service, job, snapshot)
        service.storage.stage(job, "saving_assessment")
        bundle = build_result_bundle(job, snapshot, assessment)
        result = ClaimCheckResult(
            result_case_id=bundle.case.id,
            result_finding_id=bundle.findings[0].snapshot_id,
            assessment=assessment,
            evidence_fingerprint=snapshot.fingerprint(),
            coverage="empty"
            if not snapshot.records
            else "partial"
            if snapshot.issues
            else "collected",
            sources_count=len(snapshot.records),
            issues=snapshot.issues,
        )
        service.storage.complete(job, result, bundle)
    except KeyboardInterrupt:
        service.storage.fail(job, "Worker interrupted; retry this check explicitly.")
        raise
    except StaleClaimCheck as exc:
        try:
            service.storage.fail(job, str(exc), stale=True)
        except StaleClaimCheck:
            logger.warning("Claim-check attempt %s was already completed or reclaimed", job.id)
    except Exception as exc:
        logger.warning("Claim-check job %s failed: %s", job.id, type(exc).__name__)
        # Known validation messages are useful; provider/transport errors stay bounded and generic.
        message = (
            str(exc)
            if isinstance(exc, ValueError)
            else f"Claim check failed during processing ({type(exc).__name__}). Retry explicitly."
        )
        try:
            service.storage.fail(job, message)
        except StaleClaimCheck:
            logger.warning("Claim-check attempt %s was reclaimed; old failure ignored", job.id)
    finally:
        stop.set()
        thread.join(timeout=1)
    return True
