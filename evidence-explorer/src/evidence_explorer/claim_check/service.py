"""Application API for explicit external checks of version-pinned findings."""

from __future__ import annotations

from typing import Any

from ..config import Settings
from ..review.contracts import EvidenceBundle, Fragment, Record
from ..review.storage import ReviewRepository
from .contracts import CheckPolicy, ClaimCheckJob, ClaimCheckPayload, declared_finding_origin
from .storage import ClaimCheckStorage, StaleClaimCheck


def _origin_evidence(
    bundle: EvidenceBundle, finding_id: str
) -> tuple[list[Record], list[Fragment], list[str]]:
    """Preserve bounded original claim context without importing missing media bytes."""
    fragments = {fragment.id: fragment for fragment in bundle.fragments}
    selected_records: set[str] = set()
    selected_fragments: set[str] = set()
    for link in bundle.links:
        if link.target.type != "finding" or link.target.id != finding_id:
            continue
        selected_fragments.update(link.fragment_ids)
        if link.source.type == "record":
            selected_records.add(link.source.id)
        elif link.source.type == "fragment":
            selected_fragments.add(link.source.id)
    selected_records.update(fragments[fid].record_id for fid in selected_fragments)
    records: list[Record] = []
    limitations: list[str] = []
    for record in bundle.records:
        if record.id not in selected_records:
            continue
        if record.text is None or record.artifact_ref is not None:
            limitations.append(
                f"Original source {record.id} includes media; its artifact was not copied. "
                "Inspect the source investigation for this original context."
            )
        elif len(record.text) > 50_000 or len(records) >= 8:
            limitations.append(
                f"Original source {record.id} exceeds this check's context limits; "
                "inspect the source investigation."
            )
        else:
            records.append(record)
    retained = {record.id for record in records}
    relevant = [
        fragment
        for fragment in bundle.fragments
        if fragment.id in selected_fragments and fragment.record_id in retained
    ]
    if len(relevant) > 64:
        limitations.append("Original quoted context is limited to 64 directly linked fragments.")
    if not selected_records:
        limitations.append("The selected finding has no directly linked original source context.")
    return records, relevant[:64], limitations


class ClaimCheckService:
    def __init__(
        self,
        repository: ReviewRepository,
        settings: Settings,
        *,
        discovery: Any = None,
        collector: Any = None,
        assessor: Any = None,
    ) -> None:
        if repository.db_path != settings.db_path.resolve():
            raise ValueError("Claim checks must use the workspace's SQLite database.")
        if settings.read_only:
            raise PermissionError("Claim checks require a writable workspace.")
        self.repository = repository
        self.settings = settings
        self.discovery = discovery
        self.collector = collector
        self.assessor = assessor
        repository.initialize()
        self.storage = ClaimCheckStorage(repository.db_path)
        self.storage.initialize()

    def submit(
        self, case_id: str, finding_id: str, policy: CheckPolicy | dict | None = None
    ) -> ClaimCheckJob:
        selected_policy = CheckPolicy.model_validate(policy or {})
        bundle, fingerprint = self.repository.get_bundle_snapshot(case_id)
        finding = next((f for f in bundle.findings if f.snapshot_id == finding_id), None)
        if finding is None:
            raise ValueError("Select a finding belonging to this investigation.")
        if not finding.statement.strip() or len(finding.statement.encode("utf-8")) > 4000:
            raise ValueError("Select one factual claim containing at most 4,000 UTF-8 bytes.")
        origin_records, origin_fragments, origin_limitations = _origin_evidence(bundle, finding_id)
        payload = ClaimCheckPayload(
            source_case_id=case_id,
            source_finding_id=finding_id,
            source_case_title=bundle.case.title,
            source_case_fingerprint=fingerprint,
            source_synthetic=declared_finding_origin(finding),
            source_case_synthetic=bundle.case.synthetic,
            origin_records=origin_records,
            origin_fragments=origin_fragments,
            origin_limitations=origin_limitations,
            finding_snapshot=finding,
            policy=selected_policy,
            requested_model=self.settings.chat_model,
        )
        return self.storage.enqueue(payload)

    def _with_staleness(self, job: ClaimCheckJob, fingerprint: str) -> ClaimCheckJob:
        return job.model_copy(update={"stale": fingerprint != job.payload.source_case_fingerprint})

    def get(self, job_id: str) -> ClaimCheckJob | None:
        job = self.storage.get(job_id)
        if job is None:
            return None
        return self._with_staleness(job, self.repository.get_case_fingerprint(job.source_case_id))

    def list(
        self, case_id: str, *, finding_id: str | None = None, limit: int = 20
    ) -> list[ClaimCheckJob]:
        fingerprint = self.repository.get_case_fingerprint(case_id)
        return [
            self._with_staleness(job, fingerprint)
            for job in self.storage.list(case_id, finding_id=finding_id, limit=limit)
        ]

    def retry(self, job_id: str) -> ClaimCheckJob:
        previous = self.get(job_id)
        if previous is None:
            raise ValueError("Unknown claim-check job.")
        if previous.stale or previous.status == "stale":
            raise StaleClaimCheck("Source investigation changed; submit a fresh claim check.")
        if previous.status != "failed":
            raise ValueError(
                "Only failed checks can be retried. Submit a new check to refresh sources."
            )
        payload = previous.payload.model_copy(update={"retry_of": previous.id})
        return self.storage.enqueue(payload, snapshot=previous.snapshot)

    def recover(self) -> int:
        return self.storage.recover()

    def run_next(self) -> bool:
        from .workflow import run_next

        return run_next(self)
