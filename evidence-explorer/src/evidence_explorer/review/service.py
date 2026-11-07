"""Application operations for saved evidence and version-bound human review.

Persistence is delegated to ReviewRepository; comparison and impact calculations
use the existing domain modules. Typed operations retain JSON adapters for UI and
legacy callers. Challenge setup may probe model metadata, while queued inference
is executed by a worker. Specialist collection and package handling extend this
service in review.integrations rather than replacing its decision workflow.
"""

from __future__ import annotations

from typing import Any

from .analysis import impact_for, source_groups_for
from .contracts import EvidenceBundle
from .demo import load_demo_bundle
from .importing import ImportValidationError, import_summary, parse_import, require_text_bundle
from .models import (
    AssessmentState,
    AuditEvent,
    CaseMetadata,
    CaseSummary,
    ReviewDecision,
    ReviewHistory,
    ReviewJob,
    ReviewView,
    ScenarioState,
)
from .storage import ReviewRepository
from .verifier import (
    PROMPT_VERSION,
    RETRIEVAL_VERSION,
    CheckError,
    OllamaVerifier,
    challenge_configuration,
    openrouter_configuration,
)


class ReviewService:
    """Expose review reads and explicit commands over a supplied repository.

    Construction does not initialize storage or seed examples. Baseline creation,
    exclusions, imports, job submission, and decisions are write operations even
    when their results are returned as views or dictionaries.
    """

    def __init__(self, repository: ReviewRepository):
        self.repository = repository

    def seed_demo(self) -> str:
        return self.repository.import_bundle(load_demo_bundle())

    def import_bundle(self, data: dict[str, Any]) -> str:
        bundle = EvidenceBundle.model_validate(data)
        require_text_bundle(bundle)
        return self.repository.import_bundle(bundle)

    def preview_import(self, content: str | bytes, filename: str | None = None) -> dict[str, Any]:
        parsed = parse_import(content, filename)
        try:
            status = self.repository.preview_import(parsed.bundle)
        except ValueError:
            raise ImportValidationError(
                "Import conflicts with an immutable ID or case metadata already saved. "
                "Use new version IDs for changed content. No data was imported."
            ) from None
        return import_summary(parsed, status)

    def commit_import(
        self, content: str | bytes, filename: str | None = None, *, expected_fingerprint: str
    ) -> str:
        parsed = parse_import(content, filename)
        if parsed.bundle.fingerprint() != expected_fingerprint:
            raise ImportValidationError("The import changed after preview. Preview it again first.")
        try:
            return self.repository.import_bundle(parsed.bundle)
        except ValueError:
            raise ImportValidationError(
                "Import conflicts with an immutable ID or case metadata already saved. "
                "Use new version IDs for changed content. No data was imported."
            ) from None

    def challenge_config(self) -> dict[str, Any]:
        """Probe local model availability and read cloud configuration without inference."""
        return challenge_configuration()

    def list_case_summaries(self) -> list[CaseSummary]:
        return [CaseSummary.model_validate(case) for case in self.repository.list_cases()]

    def list_cases(self) -> list[dict[str, Any]]:
        return [case.model_dump(mode="json") for case in self.list_case_summaries()]

    def load_case(self, case_id: str) -> EvidenceBundle:
        return self.repository.get_bundle(case_id)

    def get_case(self, case_id: str) -> dict[str, Any]:
        return self.load_case(case_id).model_dump(mode="json")

    def compare_sources(self, case_id: str, left_id: str, right_id: str) -> dict[str, Any]:
        """Compare two saved text sources without changing evidence or review state."""
        from .comparison import compare_sources

        try:
            bundle = self.load_case(case_id)
        except KeyError:
            raise ValueError(
                "This saved investigation is unavailable. Choose another case."
            ) from None
        return compare_sources(bundle, left_id, right_id).model_dump(mode="json")

    def case_fingerprint(self, case_id: str) -> str:
        return self.repository.get_case_fingerprint(case_id)

    def baseline_scenario_state(self, case_id: str, finding_id: str | None = None) -> ScenarioState:
        """Get one durable baseline context per case/run when an action needs it."""
        bundle = self.load_case(case_id)
        if finding_id:
            finding = next(
                (item for item in bundle.findings if item.snapshot_id == finding_id), None
            )
            if finding is None:
                raise ValueError("Selected finding is not in this investigation")
            run_id = finding.run_id
        else:
            runs = bundle.runs
            if not runs:
                raise ValueError("This investigation has no run to review")
            run_id = runs[0].id
        return self.repository.create_scenario_state(case_id, run_id, assessment_kind="baseline")

    def baseline_scenario(self, case_id: str, finding_id: str | None = None) -> dict[str, Any]:
        return self.baseline_scenario_state(case_id, finding_id).model_dump(mode="json")

    def assessment_states(
        self, case_id: str, *, bundle: EvidenceBundle | None = None
    ) -> tuple[AssessmentState, ...]:
        """Read scenario labels without loading audit decisions or source copies."""
        bundle = bundle if bundle is not None else self.load_case(case_id)
        result: list[AssessmentState] = []
        number = 0
        for scenario, kind in self.repository.list_scenario_states(case_id):
            baseline = kind == "baseline"
            if not baseline:
                number += 1
            result.append(
                AssessmentState.model_validate(
                    {
                        **scenario.model_dump(mode="json"),
                        "label": "Baseline" if baseline else f"Scenario {number}",
                        "is_baseline": baseline,
                        "assessment_kind": kind,
                        "imported": False,
                    }
                )
            )
        local_results = (
            self.repository.jobs.completed_swarm_results(case_id)
            if any(item.status == "completed" and item.result_run_id for item in bundle.scenarios)
            else {}
        )
        for scenario in bundle.scenarios:
            raw = scenario.model_dump(mode="json")
            baseline = raw.get("assessment_kind") == "baseline"
            if not baseline:
                number += 1
            locally_computed = (
                local_results.get(
                    (scenario.id, scenario.base_run_id, scenario.result_run_id), False
                )
                if scenario.status == "completed" and scenario.result_run_id
                else False
            )
            if locally_computed:
                label = f"Locally computed assessment {number}"
            elif locally_computed is None:
                label = f"Saved assessment {number}"
            else:
                label = "Imported baseline" if baseline else f"Imported assessment {number}"
            result.append(
                AssessmentState.model_validate(
                    {
                        **raw,
                        "case_id": case_id,
                        "label": label,
                        "version": raw.get("source_scenario_version", 1),
                        "is_baseline": baseline,
                        "assessment_kind": "baseline" if baseline else "scenario",
                        # Both native results and package history remain immutable.
                        "imported": True,
                        "locally_computed": locally_computed,
                    }
                )
            )
        return tuple(result)

    def assessments(self, case_id: str) -> list[dict[str, Any]]:
        return [
            state.model_dump(mode="json", exclude_unset=True)
            for state in self.assessment_states(case_id)
        ]

    def start_scenario_state(self, case_id: str, finding_id: str | None = None) -> ScenarioState:
        if finding_id:
            finding = self.repository.get_finding(case_id, finding_id)
            if finding is None:
                raise ValueError("Selected finding is not in this investigation")
            run_id = finding.run_id
        else:
            runs = self.repository.list_runs(case_id)
            if not runs:
                raise ValueError("This investigation has no run to review")
            run_id = runs[-1].id
        return self.repository.create_scenario_state(case_id, run_id)

    def start_scenario(self, case_id: str, finding_id: str | None = None) -> dict[str, Any]:
        return self.start_scenario_state(case_id, finding_id).model_dump(mode="json")

    def load_scenario(self, scenario_id: str) -> ScenarioState:
        return self.repository.get_scenario_state(scenario_id)

    def load_events(self, case_id: str) -> tuple[AuditEvent, ...]:
        return tuple(self.repository.list_audit_events(case_id))

    def load_review_history(self, case_id: str, scenario_id: str | None = None) -> ReviewHistory:
        try:
            scenario = self.load_scenario(scenario_id) if scenario_id else None
        except KeyError:
            # Imported immutable assessments belong to the bundle, not the local
            # scenario table. Only this explicit package-history path loads it.
            view = self.load_view(case_id, scenario_id, include_events=True)
            return ReviewHistory(view.events, view.scenario, view.case_fingerprint)
        if scenario and scenario.case_id != case_id:
            raise ValueError("Scenario does not belong to the selected investigation")
        return ReviewHistory(
            events=self.load_events(case_id),
            scenario=scenario,
            case_fingerprint=self.case_fingerprint(case_id),
        )

    def load_view(
        self, case_id: str, scenario_id: str | None = None, *, include_events: bool = False
    ) -> ReviewView:
        bundle, fingerprint = self.repository.get_bundle_snapshot(case_id)
        assessments = self.assessment_states(case_id, bundle=bundle)
        scenario = next((item for item in assessments if item.id == scenario_id), None)
        if scenario is None and scenario_id:
            scenario = self.load_scenario(scenario_id)
        if scenario and scenario.case_id != case_id:
            raise ValueError("Scenario does not belong to the selected investigation")
        run_id = scenario.base_run_id if scenario else (bundle.runs[0].id if bundle.runs else None)
        baseline = next(
            (
                item
                for item in assessments
                if item.is_baseline and not item.imported and item.base_run_id == run_id
            ),
            None,
        )
        baseline = baseline or next(
            (
                item
                for item in reversed(assessments)
                if item.is_baseline and item.base_run_id == run_id
            ),
            None,
        )
        excluded = list(scenario.excluded_record_ids) if scenario else []
        return ReviewView(
            bundle=bundle,
            scenario=scenario,
            impact=impact_for(bundle, excluded),
            groups=source_groups_for(bundle),
            events=self.load_events(case_id) if include_events else (),
            case_fingerprint=fingerprint,
            assessments=assessments,
            baseline_scenario=baseline,
            is_baseline=scenario is None or getattr(scenario, "is_baseline", False),
        )

    def view(self, case_id: str, scenario_id: str | None = None) -> dict[str, Any]:
        return self.load_view(case_id, scenario_id, include_events=True).wire()

    def set_source_exclusion(
        self,
        scenario_id: str,
        record_id: str,
        excluded: bool,
        reason: str,
        expected_version: int,
    ) -> ScenarioState:
        if self.repository.get_scenario_kind(scenario_id) == "baseline":
            raise ValueError("The baseline is preserved. Create a scenario to test an exclusion.")
        return self.repository.set_exclusion_state(
            scenario_id, record_id, excluded, reason, expected_version
        )

    def exclude(
        self,
        scenario_id: str,
        record_id: str,
        excluded: bool,
        reason: str,
        expected_version: int,
    ) -> dict[str, Any]:
        return self.set_source_exclusion(
            scenario_id, record_id, excluded, reason, expected_version
        ).model_dump(mode="json")

    def queue_challenge(
        self, case_id: str, finding_id: str, scenario_id: str, mode: str = "demo"
    ) -> ReviewJob:
        """Pin review context and provider identity, then persist a worker job.

        Local mode queries Ollama metadata before enqueueing. This method does
        not generate a model assessment or save a human review decision.
        """
        if mode not in {"demo", "ollama", "openrouter"}:
            raise ValueError("Choose demo, ollama, or openrouter challenge mode")
        metadata = CaseMetadata.model_validate(self.repository.get_case_metadata(case_id))
        if mode == "demo" and not metadata.case.synthetic:
            raise ValueError("Deterministic demo checks are available only for synthetic cases")
        configuration: dict[str, Any] = {}
        if mode == "ollama":
            try:
                verifier = OllamaVerifier.from_env()
                configuration = {
                    "model": verifier.requested_model,
                    "model_identity": verifier.model,
                }
                verifier.close()
            except Exception:
                raise CheckError(
                    "Local model is unavailable. Start Ollama, install EVIDENCE_CHAT_MODEL, "
                    "and check the connection before retrying."
                ) from None
        elif mode == "openrouter":
            config = openrouter_configuration()
            if not config["ready"]:
                raise CheckError(config["message"])
            configuration = {"model": config["model"]}
        if configuration:
            configuration.update(prompt_version=PROMPT_VERSION, retrieval_version=RETRIEVAL_VERSION)
        return self.repository.enqueue_job_state(
            case_id, finding_id, scenario_id, mode, **configuration
        )

    def challenge(
        self, case_id: str, finding_id: str, scenario_id: str, mode: str = "demo"
    ) -> dict[str, Any]:
        return self.queue_challenge(case_id, finding_id, scenario_id, mode).model_dump(
            mode="json", exclude_unset=True
        )

    def load_job(self, job_id: str) -> ReviewJob | None:
        job = self.repository.get_job_state(job_id)
        if job is None:
            return None
        scenario = self.load_scenario(job.payload.scenario_id)
        stale = (
            self.case_fingerprint(job.payload.case_id) != job.payload.case_fingerprint
            or scenario.version != job.payload.scenario_version
        )
        return job.model_copy(update={"stale": stale})

    def job(self, job_id: str) -> dict[str, Any] | None:
        job = self.load_job(job_id)
        return job.model_dump(mode="json", exclude_unset=True) if job else None

    def record_decision(
        self,
        case_id: str,
        finding_id: str,
        scenario_id: str,
        disposition: str,
        reason: str,
        *,
        expected_version: int | None = None,
        expected_fingerprint: str | None = None,
    ) -> ReviewDecision:
        return self.repository.save_review_state(
            case_id,
            finding_id,
            scenario_id,
            "Analyst",
            disposition,
            reason,
            expected_version=expected_version,
            expected_fingerprint=expected_fingerprint,
        )

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
    ) -> dict[str, Any]:
        return self.record_decision(
            case_id,
            finding_id,
            scenario_id,
            disposition,
            reason,
            expected_version=expected_version,
            expected_fingerprint=expected_fingerprint,
        ).model_dump(mode="json")
