"""Application services joining specialist producers to the existing review desk."""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

from ...config import Settings
from ...db import Repository
from ..analysis import compute_impact
from ..contracts import EvidenceBundle, Review, Scenario
from ..importing import ImportValidationError, ParsedImport, decode_content, import_summary
from ..models import ReviewJob, ReviewView, ScenarioState, SourceGroup
from ..origins import claim_origin_label, claim_origin_sentence
from ..service import ReviewService
from ..storage import ReviewRepository
from ..workflow import brief_projection, evidence_fingerprint
from .artifacts import CapturedArtifact, capture_verified_artifact
from .packages import (
    CapturedCasePackage,
    export_case_package,
    install_case_artifacts,
    read_case_package,
)

_ALL_ASSESSMENTS = object()


class WorkspaceRepository(ReviewRepository):
    """Reuse review storage; expose persisted computation results for comparison."""

    def initialize(self) -> None:
        super().initialize()
        from ...planning_briefs.persistence import PlanningBriefRepository

        PlanningBriefRepository(self._connection).initialize()

    def scenario_computation(self, scenario_id: str, version: int) -> dict | None:
        job = self.get_scenario_computation(scenario_id, version)
        return job.model_dump(mode="json", exclude_unset=True) if job else None


class WorkspaceService(ReviewService):
    def __init__(
        self,
        repository: WorkspaceRepository,
        settings: Settings,
        artifact_root: Path,
    ):
        super().__init__(repository)
        self.settings = settings
        self.artifact_root = Path(artifact_root).expanduser().resolve()
        from ...collection.persistence import CollectionRepository
        from ...collection.service import CollectionService

        self.collection = CollectionService(CollectionRepository(settings.db_path))

    @property
    def planning_briefs(self):
        """Use this repository's connection, including a shared read/write snapshot."""
        from ...planning_briefs.persistence import PlanningBriefRepository
        from ...planning_briefs.service import PlanningBriefService

        return PlanningBriefService(PlanningBriefRepository(self.repository._connection))

    def _has_planning_storage(self) -> bool:
        # Plain older review snapshots remain usable without silently migrating them.
        with self.repository._connection() as connection:
            return (
                connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' "
                    "AND name='monitoring_brief_revisions'"
                ).fetchone()
                is not None
            )

    def planning_progress_facts(self, view: dict):
        """Resolve planning progress from saved local state, never browser flags."""
        from ...planning_briefs.progress import confirmed_progress_facts
        from ...use_cases.contracts import BriefScopeContext, ProgressFacts
        from .snapshots import workspace_snapshot

        projected = view.get("planning_scope")
        if projected is None:
            return ProgressFacts()
        context = BriefScopeContext.model_validate(projected)
        with workspace_snapshot(self) as snapshot:
            if not snapshot._has_planning_storage():
                return ProgressFacts()
            state = snapshot.planning_briefs.state_for_case(context.case_id)
        return confirmed_progress_facts(state, context)

    def capture_case_package(self, case_id: str) -> CapturedCasePackage:
        """Detach evidence, complete relevant planning history and head context together."""
        from .snapshots import workspace_snapshot

        with workspace_snapshot(self) as snapshot:
            bundle = snapshot.export_bundle(case_id)
            history = None
            token = None
            if snapshot._has_planning_storage():
                planner = snapshot.planning_briefs
                saved = planner.history_for_case(case_id)
                if saved.revisions:
                    history = saved
                token = planner.context_token_for_case(case_id)
            return CapturedCasePackage(bundle, history, token)

    def collection_catalog(self) -> list[dict]:
        return self.collection.catalog()

    def list_collection_sources(self) -> list[dict]:
        return self.collection.sources()

    def add_collection_source(self, payload: dict) -> str:
        return self.collection.add(payload)

    def collect_source(self, source_id: str) -> str:
        return self.collection.enqueue(source_id)

    def set_collection_enabled(self, source_id: str, enabled: bool) -> None:
        self.collection.enable(source_id, enabled)

    def collection_status(self) -> dict:
        return self.collection.status()

    def preview_collection_case(self, source_ids: list[str]) -> dict:
        return self.collection.preview_case(source_ids)

    def open_collection_case(
        self, source_ids: list[str] | None = None, *, expected_fingerprint: str | None = None
    ) -> str | None:
        if source_ids is None:
            if expected_fingerprint is not None:
                raise ValueError("Choose and preview the sources for this new case.")
            # Retain the existing programmatic snapshot operation for older callers.
            bundle = self.collection.bundle()
        else:
            if expected_fingerprint is None:
                raise ValueError("Preview the selected sources before creating a case.")
            bundle = self.collection.scoped_bundle(
                source_ids, expected_fingerprint=expected_fingerprint
            )
        return self.repository.import_bundle(bundle) if bundle is not None else None

    def case_fingerprint(self, case_id: str) -> str:
        return self.repository.get_case_fingerprint(case_id)

    def import_bundle(self, data: dict) -> str:
        bundle = EvidenceBundle.model_validate(data)
        for record in bundle.records:
            if record.artifact_ref:
                self.artifact(bundle.case.id, record.id, bundle=bundle)
        return self.repository.import_bundle(bundle)

    def import_package(self, data: bytes) -> str:
        """Commit evidence and planning together, without importing a local confirmation."""
        from ...planning_briefs.persistence import PlanningBriefRepository
        from ..persistence.bundles import BundleRepository

        parsed = read_case_package(data, self.artifact_root, install_artifacts=False)
        if parsed.planning_history is not None and not self._has_planning_storage():
            raise ValueError(
                "Initialize the workspace planning schema before importing this package."
            )
        created: tuple[Path, ...] = ()
        try:
            with self.repository._connection(write=True) as connection:

                @contextmanager
                def connected(*, write=False):
                    # Both focused repositories join the caller's one guarded commit.
                    del write
                    yield connection

                case_id = BundleRepository(connected).import_bundle(parsed.bundle)
                if parsed.planning_history is not None:
                    PlanningBriefRepository(connected).import_history(parsed.planning_history)
                created = install_case_artifacts(parsed, self.artifact_root)
            return case_id
        except BaseException:
            for path in created:
                path.unlink(missing_ok=True)
            raise

    def preview_source_import(self, content: str | bytes, filename: str | None = None) -> dict:
        """Validate a mixed-source snapshot without creating a case or review state."""
        from .sources import parse_source_import

        bundle = parse_source_import(content, filename)
        try:
            status = self.repository.preview_import(bundle)
        except ValueError:
            raise ImportValidationError(
                "This source snapshot conflicts with saved evidence. No data was imported."
            ) from None
        return import_summary(
            ParsedImport(bundle=bundle, byte_count=len(decode_content(content, filename))), status
        )

    def commit_source_import(
        self,
        content: str | bytes,
        filename: str | None = None,
        *,
        expected_fingerprint: str,
    ) -> str:
        """Persist only the exact source snapshot that passed the user's preview."""
        from .sources import parse_source_import

        bundle = parse_source_import(content, filename)
        if bundle.fingerprint() != expected_fingerprint:
            raise ImportValidationError(
                "The sources changed after validation. Validate them again before importing."
            )
        try:
            return self.repository.import_bundle(bundle)
        except ValueError:
            raise ImportValidationError(
                "This source snapshot conflicts with saved evidence. No data was imported."
            ) from None

    def seed_examples(self, *, include_swarm: bool = True) -> list[str]:
        """Seed only missing fixtures; an app restart never reruns a detector."""
        from .media import build_media_demo_bundle, media_artifact_root

        existing = {case["id"] for case in self.list_cases()}
        added = []
        if "harbor:case" not in existing:
            from ..demo import load_demo_bundle

            demo = load_demo_bundle()
            if demo.case.id not in existing:
                added.append(self.repository.import_bundle(demo))
        media = build_media_demo_bundle()
        if media.case.id not in existing:
            added.append(self.import_package(export_case_package(media, media_artifact_root())))
        if include_swarm and not any(case_id.startswith("swarm:") for case_id in existing):
            from .swarm import build_swarm_demo_bundle

            added.append(self.repository.import_bundle(build_swarm_demo_bundle()))
        return added

    def import_explorer(self) -> str:
        """Import saved articles without creating demonstration source records."""
        from .explorer import bundle_from_repository

        repository = Repository(self.settings.db_path)
        repository.initialize()
        if not repository.articles.count():
            raise ValueError(
                "No saved Explorer articles are available in this workspace. "
                "Import or collect articles in Explorer, then try again. "
                "To load fictional examples instead, run the explicit seed command."
            )
        snapshot = bundle_from_repository(repository)
        identity = hashlib.sha256(
            "\n".join(run.input_fingerprint for run in snapshot.runs).encode()
        ).hexdigest()[:20]
        snapshot = bundle_from_repository(repository, case_id=f"explorer:snapshot:{identity}")
        return self.repository.import_bundle(snapshot)

    def artifact(
        self, case_id: str, record_id: str, *, bundle: EvidenceBundle | None = None
    ) -> Path:
        """Compatibility path for local callers; HTTP delivery uses captured bytes."""
        return self.capture_artifact(case_id, record_id, bundle=bundle).path

    def capture_artifact(
        self,
        case_id: str,
        record_id: str,
        *,
        expected_hash: str | None = None,
        bundle: EvidenceBundle | None = None,
    ) -> CapturedArtifact:
        """Return exactly the bounded bytes that passed the saved hash guard."""
        bundle = bundle or self.repository.get_bundle(case_id)
        return capture_verified_artifact(bundle, self.artifact_root, record_id, expected_hash)

    def start_scenario_state(self, case_id: str, finding_id: str | None = None) -> ScenarioState:
        if finding_id:
            try:
                finding = self.repository.get_finding(case_id, finding_id)
            except ValueError:
                raise ValueError("Selected finding is not in this case.") from None
            run_id = finding.run_id
        else:
            runs = self.repository.list_runs(case_id)
            if not runs:
                raise ValueError("This case has no analysis run to review.")
            run_id = runs[0].id
        return self.repository.create_scenario_state(case_id, run_id)

    def start_scenario(self, case_id: str, finding_id: str | None = None) -> dict:
        return self.start_scenario_state(case_id, finding_id).model_dump(mode="json")

    def load_view(
        self, case_id: str, scenario_id: str | None = None, *, include_events: bool = False
    ) -> ReviewView:
        view = super().load_view(case_id, scenario_id, include_events=include_events)
        bundle, scenario = view.bundle, view.scenario
        run_id = scenario.base_run_id if scenario else (bundle.runs[0].id if bundle.runs else None)
        findings = tuple(finding for finding in bundle.findings if finding.run_id == run_id)
        visible = {finding.snapshot_id for finding in findings}
        impact = view.impact.model_copy(
            update={
                "affected_finding_ids": tuple(
                    fid for fid in view.impact.affected_finding_ids if fid in visible
                )
            }
        )
        groups = view.groups
        if getattr(bundle, "swarm_context", None):
            relevant = set()
            for finding in findings:
                relevant.update(impact.remaining_evidence.get(finding.snapshot_id, ()))
                relevant.update(finding.details.get("context_record_ids", []))
            groups = tuple(
                group.model_copy(update={"record_ids": selected})
                for group in groups
                if len(selected := tuple(rid for rid in group.record_ids if rid in relevant)) > 1
            )
        artifacts = {record.id for record in bundle.records if record.artifact_ref}
        groups += tuple(
            SourceGroup(
                id=link.id,
                kind="exact_content",
                label="Identical supplied media bytes",
                record_ids=(link.source.id, link.target.id),
                basis=link.basis,
            )
            for link in bundle.links
            if link.relation == "same_content" and {link.source.id, link.target.id} <= artifacts
        )
        computation = (
            self.repository.get_scenario_computation(scenario.id, scenario.version)
            if scenario and not getattr(scenario, "imported", False)
            else None
        )
        return replace(
            view, findings=findings, impact=impact, groups=groups, computation=computation
        )

    def view(self, case_id: str, scenario_id: str | None = None) -> dict:
        context = self.load_view(case_id, scenario_id, include_events=True)
        view = context.wire()
        # Reopened review provenance includes every saved run, before display filtering.
        complete_evidence = {
            **view["bundle"],
            "findings": [finding.model_dump(mode="json") for finding in context.bundle.findings],
        }
        view["evidence_fingerprint"] = evidence_fingerprint(complete_evidence)
        from ..saved_comparison import project_saved_comparison

        view["saved_comparison"] = project_saved_comparison(
            context.bundle,
            context.scenario.id if context.scenario else None,
            imported=bool(context.scenario and getattr(context.scenario, "imported", False)),
            locally_computed=getattr(context.scenario, "locally_computed", False),
        )
        from ...planning_briefs.progress import scope_context
        from ...planning_briefs.scope import scope_identity

        try:
            view["planning_scope"] = scope_context(scope_identity(context.bundle)).model_dump(
                mode="json"
            )
        except ValueError:
            # Unsupported legacy provenance remains inspectable, never a confirmed plan.
            view["planning_scope"] = None
        if context.scenario and not getattr(context.scenario, "imported", False):
            view.setdefault("computation", None)
        return view

    def queue_recomputation(self, case_id: str, finding_id: str, scenario_id: str) -> ReviewJob:
        scenario = self.load_scenario(scenario_id)
        if scenario.case_id != case_id:
            raise ValueError("Scenario does not belong to the selected investigation.")
        run = next(
            (
                item
                for item in self.repository.list_runs(case_id)
                if item.id == scenario.base_run_id
            ),
            None,
        )
        baseline_id = self.repository.get_swarm_baseline_run_id(case_id)
        if run is None or not run.full_inputs_available or not baseline_id:
            raise ValueError(
                "Recomputation is unavailable: the complete Swarm input and background "
                "activity are required. Evidence inspection remains available."
            )
        if run.id != baseline_id:
            raise ValueError(
                "Return to the original Swarm assessment to create another comparison."
            )
        if not scenario.excluded_record_ids:
            raise ValueError("Exclude an episode or source before running the comparison.")
        return self.repository.enqueue_job_state(
            case_id, finding_id, scenario_id, "swarm_recompute"
        )

    def recompute(self, case_id: str, finding_id: str, scenario_id: str) -> dict:
        return self.queue_recomputation(case_id, finding_id, scenario_id).model_dump(
            mode="json", exclude_unset=True
        )

    def challenge(
        self, case_id: str, finding_id: str, scenario_id: str, mode: str = "demo"
    ) -> dict:
        return self.queue_challenge(case_id, finding_id, scenario_id, mode).model_dump(
            mode="json", exclude_unset=True
        )

    def exclude(
        self,
        scenario_id: str,
        record_id: str,
        excluded: bool,
        reason: str,
        expected_version: int,
    ) -> dict:
        return super().exclude(scenario_id, record_id, excluded, reason, expected_version)

    def review(
        self,
        case_id: str,
        finding_id: str,
        scenario_id: str,
        disposition: str,
        reason: str,
        *,
        expected_version: int | None = None,
        expected_fingerprint: str | None = None,
    ) -> dict:
        return self.repository.save_review(
            case_id,
            finding_id,
            scenario_id,
            "Analyst",
            disposition,
            reason,
            expected_version=expected_version,
            expected_fingerprint=expected_fingerprint,
        )

    def load_job(self, job_id: str) -> ReviewJob | None:
        job = super().load_job(job_id)
        if job and job.mode == "swarm_recompute":
            scenario = self.load_scenario(job.scenario_id)
            return job.model_copy(
                update={"stale": scenario.version != job.payload.scenario_version}
            )
        return job

    def job(self, job_id: str) -> dict | None:
        job = self.load_job(job_id)
        return job.model_dump(mode="json", exclude_unset=True) if job else None

    def export_bundle(self, case_id: str) -> EvidenceBundle:
        """Include every reviewed scenario version, rather than only its latest state."""
        data = self.repository.get_bundle(case_id).model_dump(mode="json")
        source_fingerprint = self.case_fingerprint(case_id)
        source_evidence_fingerprint = evidence_fingerprint(data)
        findings = {finding["snapshot_id"]: finding for finding in data["findings"]}
        scenario_states: dict[str, dict] = {}
        known_scenarios = {scenario["id"] for scenario in data["scenarios"]}
        known_reviews = {review["id"] for review in data["reviews"]}
        for event in self.repository.list_events(case_id):
            payload = event["payload"]
            sid = event["scenario_id"]
            if event["event_type"] == "scenario_created":
                scenario_states[sid] = {
                    "base_run_id": payload["base_run_id"],
                    "excluded": set(),
                    "version": 1,
                    "assessment_kind": payload.get("assessment_kind", "scenario"),
                }
            state = scenario_states.get(sid)
            if state is None:
                continue
            if event["event_type"] == "exclusion_changed":
                if payload["excluded"]:
                    state["excluded"].add(payload["record_id"])
                else:
                    state["excluded"].discard(payload["record_id"])
                state["version"] = payload["scenario_version"]
            version = payload.get("scenario_version", state["version"])
            exported_sid = f"review:{sid}:v{version}"
            if exported_sid not in known_scenarios:
                computation = self.repository.scenario_computation(sid, version)
                result = (computation or {}).get("result") or {}
                data["scenarios"].append(
                    Scenario(
                        id=exported_sid,
                        base_run_id=state["base_run_id"],
                        excluded_record_ids=sorted(state["excluded"]),
                        reason=payload.get("reason", "Saved reviewer scenario"),
                        status="completed" if result.get("result_run_id") else "dependency_preview",
                        result_run_id=result.get("result_run_id"),
                        source_scenario_id=sid,
                        source_scenario_version=version,
                        assessment_kind=state["assessment_kind"],
                    ).model_dump(mode="json")
                )
                known_scenarios.add(exported_sid)
            if event["event_type"] == "finding_reviewed" and payload["id"] not in known_reviews:
                finding = findings[payload["finding_id"]]
                data["reviews"].append(
                    Review(
                        id=payload["id"],
                        finding_snapshot_id=payload["finding_id"],
                        run_id=finding["run_id"],
                        scenario_id=exported_sid,
                        reviewer=payload["reviewer"],
                        disposition=payload["disposition"],
                        reason=payload["reason"],
                        timestamp=payload["created_at"],
                        origin="analyst",
                        case_fingerprint=payload.get("case_fingerprint"),
                        source_case_fingerprint=source_fingerprint,
                        evidence_fingerprint=source_evidence_fingerprint,
                    ).model_dump(mode="json")
                )
                known_reviews.add(payload["id"])
        return EvidenceBundle.model_validate(data)

    def export_package(self, case_id: str) -> bytes:
        captured = self.capture_case_package(case_id)
        return export_case_package(
            captured.bundle, self.artifact_root, planning_history=captured.planning_history
        )

    def export_brief(
        self, case_id: str, scenario_id: str | None | object = _ALL_ASSESSMENTS
    ) -> str:
        """Export the selected assessment, or legacy audit history if omitted."""
        if scenario_id is not _ALL_ASSESSMENTS:
            from .briefs import render_brief

            if scenario_id is not None and not isinstance(scenario_id, str):
                raise ValueError("Choose a saved assessment or the baseline.")
            return render_brief(brief_projection(self.view(case_id, scenario_id)))
        return self._export_review_history(case_id)

    def _export_review_history(self, case_id: str) -> str:
        bundle = self.export_bundle(case_id)

        evidence = compute_impact(bundle, [])["remaining_evidence"]
        records = {record.id: record for record in bundle.records}
        runs = {run.id: run for run in bundle.runs}
        lines = [f"# {bundle.case.title}", "", bundle.case.question, ""]
        if bundle.case.synthetic:
            lines += [
                "**Selected case: Synthetic example.**\n"
                "This label applies to the selected case. Saved records retain their own "
                "provenance; collected sources are not labeled fictional.",
                "",
            ]
        for finding in bundle.findings:
            lines += [
                f"## {finding.kind.title()}",
                "",
                "> " + finding.statement,
                "",
                f"Snapshot: `{finding.snapshot_id}` · Run: `{finding.run_id}`",
                *(
                    [claim_origin_sentence(origin)]
                    if (origin := claim_origin_label({"details": finding.details}))
                    else []
                ),
                "",
            ]
            reviews = [r for r in bundle.reviews if r.finding_snapshot_id == finding.snapshot_id]
            if not reviews:
                lines += ["Reviewer decision: not reviewed.", ""]
            for review in reviews:
                lines += [
                    f"Decision: **{review.disposition}** · {review.reviewer} · {review.timestamp}",
                    "> " + review.reason.replace("\n", "\n> "),
                    f"Scenario: `{review.scenario_id or 'baseline'}`",
                    "",
                ]
            lines += ["Evidence references:", ""]
            record_ids = evidence.get(finding.snapshot_id, [])
            for record_id in record_ids[:12]:
                record = records[record_id]
                source = (
                    record.source_url or record.artifact_ref or record.metadata.get("source_ref")
                )
                lines.append(
                    f"- `{record.id}` — {record.source_label}; "
                    f"source: {source or 'supplied saved record'}; "
                    f"published: {record.published_at or 'unknown'}"
                )
                provenance = record.metadata.get("synthetic")
                origin = (
                    "Synthetic record"
                    if provenance is True
                    else "Collected source; factual accuracy requires review"
                    if provenance is False
                    else "Record origin not specified"
                )
                lines.append(f"  Record provenance: {origin}.")
            if not record_ids:
                lines.append("- No supporting record linked; inspect the stated assumption.")
            if len(record_ids) > 12:
                lines.append(
                    f"- {len(record_ids) - 12} further records are included in the case package."
                )
            lines.append("")
        lines += ["## Analysis methods", ""]
        for run in runs.values():
            lines += [
                f"- `{run.id}`: {run.method}, version {run.method_version}",
                f"  - Input fingerprint: `{run.input_fingerprint}`",
                "  - Configuration: `" + json.dumps(run.configuration, sort_keys=True) + "`",
                "  - Coverage: `" + json.dumps(run.coverage, sort_keys=True) + "`",
                "",
            ]
        lines += [
            "## Coverage and limitations",
            "",
            "Case coverage: `" + json.dumps(bundle.case.coverage, sort_keys=True) + "`",
            "",
            *[f"- {x}" for x in bundle.case.limitations],
            "",
            "The accompanying case package contains exact evidence, analysis runs, "
            "scenario history, and source references.",
            "",
        ]
        return "\n".join(lines)
