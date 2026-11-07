"""External checks preserve evidence, review history, and failure semantics."""

import hashlib
from datetime import UTC, datetime

import pytest

from evidence_explorer.claim_check.contracts import (
    AssessmentCitation,
    CheckPolicy,
    ProposedAssessment,
    SearchBatch,
    SearchHit,
)
from evidence_explorer.claim_check.service import ClaimCheckService
from evidence_explorer.claim_check.storage import StaleClaimCheck
from evidence_explorer.config import Settings
from evidence_explorer.ingestion.models import CollectedArticle, CollectionError, SourceProvenance
from evidence_explorer.models import ArticleInput
from evidence_explorer.review.contracts import (
    Case,
    EvidenceBundle,
    Finding,
    Fragment,
    Link,
    ObjectRef,
    Record,
    Run,
)
from evidence_explorer.review.integrations.workspace import WorkspaceRepository, WorkspaceService

NOW = "2026-11-06T12:00:00+00:00"
SOURCE_TEXT = "The observatory opened on 5 November 2026. Its first visitor arrived that morning."


class Discovery:
    def __init__(self, urls=None):
        self.urls = urls if urls is not None else ["https://example.org/observatory"]
        self.calls = 0

    def search(self, query, *, limit):
        self.calls += 1
        return SearchBatch(
            query=query,
            provider="test-discovery",
            searched_at=NOW,
            hits=[
                SearchHit(url=url, title="Source lead", query=query, rank=index + 1)
                for index, url in enumerate(self.urls[:limit])
            ],
        )


class Collector:
    def __init__(self):
        self.calls = 0

    def collect_article(self, url, *, topic):
        self.calls += 1
        if url.endswith("/missing"):
            raise CollectionError("Source unavailable.", code="fetch_failed", url=url)
        return CollectedArticle(
            article=ArticleInput(
                title="Observatory opening record",
                publisher="Example observatory",
                url=url,
                published_at="",
                text=SOURCE_TEXT,
                topic=topic,
            ),
            provenance=SourceProvenance(
                requested_url=url, final_url=url, retrieved_at=NOW, published_at=None
            ),
        )


class Assessor:
    def __init__(self, *, invalid=False, before=None):
        self.calls = 0
        self.invalid = invalid
        self.before = before

    def assess(self, claim, snapshot):
        self.calls += 1
        if self.before:
            self.before()
        fragment = snapshot.fragments[0]
        return ProposedAssessment(
            status="supported",
            explanation="The saved source states this opening date; an analyst must review it.",
            citations=[
                AssessmentCitation(
                    fragment_id=fragment.id,
                    quote="Invented source quote" if self.invalid else fragment.text,
                    stance="supports",
                )
            ],
            model="test-model@fixed",
            provider="test-provider",
            prompt_version="test-v1",
        )


def original_bundle():
    return EvidenceBundle(
        case=Case(id="source-case", title="Source investigation", question="When did it open?"),
        records=[
            Record(
                id="original-record",
                kind="text",
                source_label="Supplied claim",
                text=SOURCE_TEXT,
                content_hash=hashlib.sha256(SOURCE_TEXT.encode()).hexdigest(),
            )
        ],
        fragments=[],
        runs=[
            Run(
                id="original-run",
                input_fingerprint="input-v1",
                input_manifest=["original-record"],
                method="supplied",
                method_version="1",
                created_at=NOW,
            )
        ],
        findings=[
            Finding(
                snapshot_id="original-finding",
                lineage_id="opening-date",
                version=1,
                run_id="original-run",
                kind="inference",
                statement="The observatory opened on 5 November 2026.",
            )
        ],
    )


@pytest.fixture
def service(tmp_path):
    settings = Settings(data_dir=tmp_path)
    repository = WorkspaceRepository(settings.db_path)
    repository.initialize()
    repository.import_bundle(original_bundle())
    return ClaimCheckService(
        repository,
        settings,
        discovery=Discovery(),
        collector=Collector(),
        assessor=Assessor(),
    )


def submit(service):
    return service.submit("source-case", "original-finding", CheckPolicy(queries=["opening date"]))


def append_source_revision(service):
    bundle = service.repository.get_bundle("source-case")
    payload = bundle.model_dump(mode="json")
    payload["findings"].append(
        {**payload["findings"][0], "snapshot_id": "later-finding", "version": 2}
    )
    service.repository.import_bundle(EvidenceBundle.model_validate(payload))


def test_result_preserves_source_freezes_before_model_and_survives_review_export(service, tmp_path):
    original = service.repository.get_case_fingerprint("source-case")
    queued = submit(service)
    assert submit(service).id == queued.id
    observed_snapshots = []
    service.assessor.before = lambda: observed_snapshots.append(service.get(queued.id).snapshot)
    assert service.run_next() is True
    assert service.run_next() is False
    job = service.get(queued.id)
    assert job.status == "completed" and job.stage == "awaiting_review"
    assert observed_snapshots[0].fingerprint() == job.snapshot.fingerprint()
    assert job.result.assessment.status == "supported"
    assert job.result.result_case_id != "source-case"
    assert service.repository.get_case_fingerprint("source-case") == original
    assert job.snapshot.records[0].published_at is None

    workspace = WorkspaceService(service.repository, service.settings, tmp_path / "evidence")
    result_case = job.result.result_case_id
    scenario = workspace.start_scenario(result_case)
    decision = workspace.review(
        result_case,
        job.result.result_finding_id,
        scenario["id"],
        "qualify",
        "One fetched source supports the date; independence remains unreviewed.",
    )
    package = workspace.export_package(result_case)
    restored_settings = Settings(data_dir=tmp_path / "restored")
    restored_repository = WorkspaceRepository(restored_settings.db_path)
    restored_repository.initialize()
    restored = WorkspaceService(
        restored_repository, restored_settings, tmp_path / "restored" / "evidence"
    )
    assert restored.import_package(package) == result_case
    reopened = restored.repository.get_bundle(result_case)
    assert any(review.id == decision["id"] for review in reopened.reviews)
    assert reopened.records[0].text == SOURCE_TEXT
    assert reopened.findings[0].details["assessment"]["status"] == "supported"
    reloaded = ClaimCheckService(service.repository, service.settings)
    assert reloaded.get(job.id).snapshot == job.snapshot


def test_partial_collection_is_not_reported_as_complete_or_hidden(service):
    service.discovery = Discovery(
        ["https://example.org/missing", "https://example.org/observatory"]
    )
    queued = submit(service)
    service.run_next()
    job = service.get(queued.id)
    assert job.status == "completed"
    assert job.result.coverage == "partial"
    assert [issue.code for issue in job.result.issues] == ["fetch_failed"]
    assert job.result.sources_count == 1
    assert len(job.snapshot.attempted_urls) == 2
    assert service.assessor.calls == 1


def test_no_source_evidence_returns_insufficient_without_calling_model(service):
    service.discovery = Discovery(["https://example.org/missing"])
    queued = submit(service)
    service.run_next()
    job = service.get(queued.id)
    assert job.status == "completed"
    assert job.result.coverage == "empty"
    assert job.result.assessment.status == "insufficient_evidence"
    assert job.result.assessment.citations == []
    assert job.result.assessment.metadata["model_called"] is False
    assert service.assessor.calls == 0


def test_invalid_citations_fail_and_explicit_retry_reuses_identical_frozen_sources(service):
    service.assessor.invalid = True
    queued = submit(service)
    service.run_next()
    failed = service.get(queued.id)
    assert failed.status == "failed"
    assert "citation" in failed.error.lower()
    assert failed.result is None and failed.snapshot.records
    assert len(service.repository.list_cases()) == 1
    network_calls = (service.discovery.calls, service.collector.calls)
    service.assessor.invalid = False
    retry = service.retry(queued.id)
    assert retry.id != queued.id and retry.payload.retry_of == queued.id
    service.run_next()
    completed = service.get(retry.id)
    assert completed.status == "completed"
    assert completed.snapshot.fingerprint() == failed.snapshot.fingerprint()
    assert (service.discovery.calls, service.collector.calls) == network_calls
    assert service.get(queued.id).status == "failed"


@pytest.mark.parametrize("during_assessment", [False, True])
def test_changed_source_prevents_results_from_being_applied(service, during_assessment):
    queued = submit(service)
    if during_assessment:
        service.assessor.before = lambda: append_source_revision(service)
    else:
        append_source_revision(service)
    service.run_next()
    job = service.get(queued.id)
    assert job.status == "stale" and job.stale is True
    assert job.result is None
    assert len(service.repository.list_cases()) == 1
    if not during_assessment:
        assert service.discovery.calls == service.collector.calls == service.assessor.calls == 0
    with pytest.raises(StaleClaimCheck, match="fresh"):
        service.retry(job.id)


def test_result_import_and_job_completion_are_atomic(service, monkeypatch):
    from evidence_explorer.claim_check.storage import BundleRepository

    import_bundle = BundleRepository.import_bundle

    def interrupted_import(repository, bundle):
        import_bundle(repository, bundle)
        raise ValueError("Simulated failure after result insertion")

    monkeypatch.setattr(BundleRepository, "import_bundle", interrupted_import)
    queued = submit(service)
    service.run_next()
    job = service.get(queued.id)
    assert job.status == "failed" and job.result is None
    assert len(service.repository.list_cases()) == 1


def test_expired_attempt_requires_explicit_retry_and_cannot_overwrite_it(service):
    queued = submit(service)
    claimed = service.storage.claim()
    assert claimed.id == queued.id
    with service.storage.connection(write=True) as connection:
        connection.execute(
            "UPDATE claim_check_jobs SET lease_expires_at=? WHERE id=?",
            (datetime(2000, 1, 1, tzinfo=UTC).isoformat(), claimed.id),
        )
    assert service.recover() == 1
    assert service.run_next() is False
    with pytest.raises(StaleClaimCheck, match="attempt"):
        service.storage.stage(claimed, "assessing_claim")
    retry = service.retry(claimed.id)
    assert service.run_next() is True
    assert service.get(retry.id).status == "completed"
    assert service.get(claimed.id).status == "failed"


def test_synthetic_source_label_survives_the_external_check(service):
    bundle = original_bundle().model_dump(mode="json")
    bundle["case"].update(id="synthetic-source", synthetic=True)
    bundle["findings"][0]["details"]["synthetic"] = True
    service.repository.import_bundle(EvidenceBundle.model_validate(bundle))
    queued = service.submit("synthetic-source", "original-finding")
    service.run_next()
    job = service.get(queued.id)
    assert job.payload.source_synthetic is True
    assert service.repository.get_bundle(job.result.result_case_id).case.synthetic is True


def test_completed_history_becomes_stale_after_source_expands(service):
    queued = submit(service)
    service.run_next()
    result_id = service.get(queued.id).result.result_case_id
    saved = service.repository.get_case_fingerprint(result_id)
    append_source_revision(service)
    assert service.get(queued.id).stale is True
    assert service.list("source-case")[0].stale is True
    assert service.repository.get_case_fingerprint(result_id) == saved


def test_submission_keeps_exact_original_claim_passage_separate_from_new_evidence(service):
    bundle = original_bundle()
    original_quote = SOURCE_TEXT.split(". ")[0] + "."
    fragment = Fragment(
        id="original-quote",
        record_id="original-record",
        selector={"kind": "text", "start": 0, "end": len(original_quote)},
        quote=original_quote,
    )
    bundle.fragments.append(fragment)
    bundle.links.append(
        Link(
            id="claim-origin-link",
            source=ObjectRef(type="fragment", id=fragment.id),
            target=ObjectRef(type="finding", id="original-finding"),
            relation="supports",
            fragment_ids=[fragment.id],
            basis="Original extracted claim context, not external corroboration.",
        )
    )
    service.repository.import_bundle(bundle)
    job = submit(service)
    assert job.payload.origin_records[0].text == SOURCE_TEXT
    assert job.payload.origin_fragments == [fragment]
    assert job.payload.origin_limitations == []
    service.run_next()
    completed = service.get(job.id)
    result = service.repository.get_bundle(completed.result.result_case_id)
    assert len(result.records) == len(completed.snapshot.records) + 1
    assert any(item.quote == original_quote for item in result.fragments)
    assert len(completed.result.assessment.citations) == 1


def test_media_origin_remains_explicitly_unavailable_without_copying_missing_artifact(service):
    bundle = original_bundle()
    bundle.records.append(
        Record(
            id="source-image",
            kind="image",
            source_label="Original photograph",
            artifact_ref="frames/photo.png",
            content_hash="abc",
        )
    )
    bundle.links.append(
        Link(
            id="image-origin",
            source=ObjectRef(type="record", id="source-image"),
            target=ObjectRef(type="finding", id="original-finding"),
            relation="supports",
            basis="Supplied media context.",
        )
    )
    service.repository.import_bundle(bundle)
    queued = submit(service)
    assert queued.payload.origin_records == []
    assert "media" in queued.payload.origin_limitations[0]
    service.run_next()
    job = service.get(queued.id)
    result = service.repository.get_bundle(job.result.result_case_id)
    assert all(record.artifact_ref is None for record in result.records)
    assert any("media" in message for message in result.case.limitations)


def test_later_queries_receive_collection_slots_even_with_six_seed_sources(service):
    class MultiQueryDiscovery:
        def search(self, query, *, limit):
            return SearchBatch(
                query=query,
                provider="test-search",
                searched_at=NOW,
                hits=[
                    SearchHit(
                        url=f"https://example.org/{query}/{index}",
                        title=query,
                        query=query,
                        rank=index + 1,
                    )
                    for index in range(limit)
                ],
            )

    service.discovery = MultiQueryDiscovery()
    policy = CheckPolicy(
        queries=["primary", "contradiction", "context"],
        seed_urls=[f"https://example.org/seed/{index}" for index in range(6)],
        max_sources=6,
        results_per_query=5,
    )
    queued = service.submit("source-case", "original-finding", policy)
    service.run_next()
    job = service.get(queued.id)
    assert job.status == "completed"
    attempted = job.snapshot.attempted_urls
    assert len(attempted) == 6
    assert attempted[:4] == [
        "https://example.org/seed/0",
        "https://example.org/primary/0",
        "https://example.org/contradiction/0",
        "https://example.org/context/0",
    ]
    assert job.result.coverage == "partial"
    budget = next(issue for issue in job.result.issues if issue.code == "collection_budget")
    assert budget.message.startswith("15 discovered unique URLs were not fetched")


def test_history_filters_the_finding_before_applying_its_limit(service):
    first = submit(service)
    append_source_revision(service)
    newer = service.submit("source-case", "later-finding")
    assert service.list("source-case", limit=1)[0].id == newer.id
    assert service.list("source-case", finding_id="original-finding", limit=1)[0].id == first.id


def test_mixed_claim_origin_brief_and_package_keep_collected_provenance(service, tmp_path):
    bundle = original_bundle()
    bundle.case.id = "test-claim-source"
    bundle.case.synthetic = True
    bundle.findings[0].details["synthetic"] = True
    service.repository.import_bundle(bundle)
    job = service.submit(bundle.case.id, bundle.findings[0].snapshot_id)
    service.run_next()
    completed = service.get(job.id)
    assert completed.status == "completed"
    workspace = WorkspaceService(service.repository, service.settings, tmp_path / "artifacts")
    case_id = completed.result.result_case_id
    scenario = workspace.baseline_scenario_state(case_id, completed.result.result_finding_id)
    workspace.review(
        case_id,
        completed.result.result_finding_id,
        scenario.id,
        "qualify",
        "Test claim; review collected evidence.",
    )
    brief = workspace.export_brief(case_id, scenario_id=scenario.id)
    assert "Synthetic test claim" in brief
    assert "fictional evidence and findings" not in brief
    assert "Collected source; factual accuracy requires review" in brief
    package = workspace.export_package(case_id)
    target_settings = Settings(data_dir=tmp_path / "mixed-reopen")
    target_repo = WorkspaceRepository(target_settings.db_path)
    target_repo.initialize()
    target = WorkspaceService(target_repo, target_settings, target_settings.data_dir / "artifacts")
    target.import_package(package)
    reopened = target.repository.get_bundle(case_id)
    assert reopened.case.synthetic is True
    assert reopened.findings[0].details["claim_check"]["source_synthetic"] is True
    assert any(r.metadata.get("synthetic") is False for r in reopened.records)
    reopened_brief = target.export_brief(case_id, scenario_id=reopened.reviews[0].scenario_id)
    assert "Synthetic test claim" in reopened_brief
    assert "fictional evidence and findings" not in reopened_brief
