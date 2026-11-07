"""Explicit harmless ACT-01 verification workspace; never use an operational store.

Example: python scripts/activity_preview.py --data-dir /tmp/activity-fixture --seed --port 56013
No worker, provider or model is started. Seeding is refused if a database exists.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from evidence_explorer.config import Settings
from evidence_explorer.planning_briefs.models import DraftFields
from evidence_explorer.review.contracts import EvidenceBundle
from evidence_explorer.review.integrations.web import create_app
from evidence_explorer.review.integrations.workspace import WorkspaceRepository, WorkspaceService
from evidence_explorer.use_cases.contracts import AlertRule, WindowDefinition

START = datetime(2026, 11, 6, 10, tzinfo=UTC)
BASELINE = WindowDefinition(id="baseline", start=START, end=START + timedelta(hours=1))
OBSERVATION = WindowDefinition(id="observation", start=BASELINE.end, end=START + timedelta(hours=2))
RULE = AlertRule(
    id="fixture:explicit-30-percent",
    title="Controlled saved-sample comparison",
    metric="unique saved record snapshots of the selected kind",
    threshold_percent=30,
    threshold_basis="analyst_configured",
    direction="increase",
    comparison_basis="equal_duration_counts",
    min_baseline_count=5,
    min_current_count=0,
    min_coverage_fraction=1,
)


def activity_fixture(
    name: str, baseline_count: int, observation_count: int, *, known_coverage: bool = True
) -> EvidenceBundle:
    """A declared closed fictional corpus, not an observed platform activity trend."""
    records = []
    for window_name, count, start in (
        ("baseline", baseline_count, BASELINE.start),
        ("observation", observation_count, OBSERVATION.start),
        ("outside", 1, OBSERVATION.end),
    ):
        for index in range(count):
            rid = f"activity:{name}:{window_name}:{index + 1}"
            text = (
                f"Fictional geometric-card notice {window_name} {index + 1}. "
                "Controlled activity fixture; no real person, platform or event."
            )
            records.append(
                {
                    "id": rid,
                    "kind": "saved_source_item",
                    "source_label": "Synthetic closed corpus",
                    "text": text,
                    "content_hash": hashlib.sha256(text.encode()).hexdigest(),
                    "published_at": (start + timedelta(minutes=index)).isoformat(),
                    "acquired_at": "2026-11-07T00:00:00+00:00",
                    "metadata": {"synthetic": True, "fixture_window": window_name},
                }
            )
    refs = sorted(row["id"] for row in records)
    coverage = {
        "fixture": "Explicit synthetic closed corpus; not live-platform coverage.",
        "full_source_coverage": False,
    }
    if known_coverage:
        coverage["saved_activity_coverage"] = {
            "policy": "saved-activity-coverage/v1",
            "count_unit": "saved_record_snapshots",
            "record_kind": "saved_source_item",
            "timestamp_basis": "publication",
            "source_refs": refs,
            **{
                key: {
                    "start": window.start.isoformat(),
                    "end": window.end.isoformat(),
                    "coverage_status": "complete",
                    "coverage_fraction": 1,
                    "evidence": (
                        f"Synthetic fixture generator supplies every record in this closed {key} "
                        "sample. This is a saved declaration, not independent platform coverage."
                    ),
                }
                for key, window in (("baseline", BASELINE), ("observation", OBSERVATION))
            },
        }
    run_id = f"activity:{name}:run"
    finding_id = f"activity:{name}:finding"
    return EvidenceBundle.model_validate(
        {
            "case": {
                "id": f"activity:{name}",
                "title": {
                    "cue": "Synthetic activity · 5 to 7 saved records",
                    "below": "Synthetic activity · 5 to 6 saved records",
                    "unknown": "Synthetic activity · coverage unknown",
                    "zero": "Synthetic activity · zero baseline",
                }[name],
                "question": "What changes in this explicitly bounded fictional record sample?",
                "synthetic": True,
                "coverage": coverage,
                "limitations": [
                    "Controlled verification fixture. No real-world trend or monitoring claim.",
                    "Publication timestamps are fictional source declarations, not event times.",
                ],
            },
            "records": records,
            "fragments": [],
            "runs": [
                {
                    "id": run_id,
                    "input_fingerprint": hashlib.sha256(json.dumps(refs).encode()).hexdigest(),
                    "input_manifest": refs,
                    "method": "Explicit saved activity fixture",
                    "method_version": "1",
                    "created_at": "2026-11-07T00:00:00+00:00",
                    "full_inputs_available": True,
                }
            ],
            "findings": [
                {
                    "snapshot_id": finding_id,
                    "lineage_id": finding_id,
                    "version": 1,
                    "run_id": run_id,
                    "kind": "observation",
                    "statement": "The saved corpus contains explicitly labelled fictional notices.",
                }
            ],
            "links": [
                {
                    "id": f"activity:{name}:support:{index}",
                    "source": {"type": "record", "id": rid},
                    "target": {"type": "finding", "id": finding_id},
                    "relation": "supports",
                    "basis": "Exact supplied fictional text; no activity interpretation implied.",
                    "run_id": run_id,
                }
                for index, rid in enumerate(refs)
            ],
        }
    )


def seed_workspace(service: WorkspaceService) -> None:
    for name, baseline, observation, known in (
        ("cue", 5, 7, True),
        ("below", 5, 6, True),
        ("unknown", 5, 7, False),
        ("zero", 0, 7, True),
    ):
        bundle = activity_fixture(name, baseline, observation, known_coverage=known)
        service.import_bundle(bundle.model_dump(mode="json"))
        planner = service.planning_briefs
        binding = planner.case_binding(bundle.case.id)
        draft = planner.create_draft(
            author="Synthetic fixture author",
            fields=DraftFields(
                use_case_id="public_information_monitoring",
                purpose="Compare the declared fictional record windows manually.",
                public_subjects_or_topics=("Fictional geometric-card notices",),
                source_selection=binding.scope.source_refs,
                reviewer_role="Fixture reviewer",
                expected_output="reviewed_brief",
                baseline_window=BASELINE,
                observation_window=OBSERVATION,
                alert_rule=RULE,
            ),
        )
        bound = planner.bind(
            draft.brief_id,
            bundle.case.id,
            expected_head_revision_id=draft.revision_id,
            expected_case_fingerprint=bundle.fingerprint(),
            author="Synthetic fixture author",
        )
        planner.confirm(
            draft.brief_id,
            expected_head_revision_id=bound.revision_id,
            expected_case_fingerprint=bundle.fingerprint(),
            reviewer="Fixture reviewer",
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", required=True, type=Path)
    parser.add_argument("--seed", action="store_true", help="Create explicit fictional cases")
    parser.add_argument("--port", type=int, default=56013)
    args = parser.parse_args()
    settings = Settings(data_dir=args.data_dir.resolve(), ollama_url="http://127.0.0.1:1")
    if args.seed and settings.db_path.exists():
        parser.error("Seeding requires a new empty directory; an existing database is preserved.")
    repository = WorkspaceRepository(settings.db_path)
    repository.initialize()
    service = WorkspaceService(repository, settings, settings.data_dir / "artifacts")
    if args.seed:
        seed_workspace(service)
    app = create_app(service)
    print(json.dumps({"url": f"http://127.0.0.1:{args.port}/", "data_dir": str(settings.data_dir)}))
    app.run(host="127.0.0.1", port=args.port, debug=False)


if __name__ == "__main__":
    main()
