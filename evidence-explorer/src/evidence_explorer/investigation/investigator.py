"""Run a few explicit Maltego lookups and draft claims from the frozen records.

Credit discipline: ``start`` uses only free calls (search and describe), and
``validate`` asks the server to check entitlement and input without executing
(no credits). ``lookup`` spends shared credits and runs only the capabilities
it is given. Every Data MCP call is frozen by the Maltego data source; the
investigation folder records which records each step produced, the credit
figures paid runs reported, and the model that drafted the claims.

An investigation folder lives at ``<data dir>/investigations/<stamp>-<slug>/``
next to the frozen records it cites. Like the record store, the folder ignores
itself in git: it holds investigation data.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evidence_explorer.collection.maltego.records import MaltegoLookupRecord, RecordStore

from . import INVESTIGATOR_VERSION
from .bundle import (
    InvestigationDraft,
    build_investigation_bundle,
    digest,
    record_labels,
    source_bundle,
)
from .model import DRAFT_PROMPT_VERSION, StructuredModel, write_draft

MANIFEST = "investigation.json"
SCHEMA = "traced.investigation/1"


def now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def slug(text: str) -> str:
    words = re.findall(r"[a-z0-9]+", text.casefold())
    return "-".join(words)[:48].strip("-") or "investigation"


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


@dataclass(frozen=True)
class Candidate:
    capability_id: str
    name: str
    input_type: str
    source: str | None
    question: str | None
    score: float | None
    rank: int

    def to_json(self) -> dict:
        return dict(self.__dict__)


def parse_candidates(
    record: MaltegoLookupRecord, entity_type: str
) -> tuple[list[Candidate], str | None]:
    """Ranked capabilities for the subject's type from a frozen search record."""
    payload = record.payload() or {}
    impression = (
        payload.get("impression_id") if isinstance(payload.get("impression_id"), str) else None
    )
    candidates: list[Candidate] = []
    seen: set[str] = set()
    matches = (
        payload.get("objective_matches")
        if isinstance(payload.get("objective_matches"), dict)
        else {}
    )
    for result in matches.get("results") or []:
        if not isinstance(result, dict):
            continue
        for variant in result.get("target_variants") or []:
            if not isinstance(variant, dict):
                continue
            capability = variant.get("catalog_id")
            if not isinstance(capability, str) or capability in seen:
                continue
            if variant.get("input_type") not in {None, entity_type}:
                continue
            seen.add(capability)
            candidates.append(
                Candidate(
                    capability,
                    str(variant.get("display_name") or capability),
                    str(variant.get("input_type") or entity_type),
                    result.get("source") if isinstance(result.get("source"), str) else None,
                    result.get("question") or None,
                    result.get("score") if isinstance(result.get("score"), int | float) else None,
                    int(result.get("rank") or len(candidates) + 1),
                )
            )
    facets = payload.get("facets") if isinstance(payload.get("facets"), dict) else {}
    for item in facets.get("popular_for_type") or []:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            continue
        if item["id"] in seen or entity_type not in (item.get("input_types") or []):
            continue
        seen.add(item["id"])
        candidates.append(
            Candidate(
                item["id"],
                str(item.get("name") or item["id"]).strip(),
                entity_type,
                None,
                None,
                None,
                len(candidates) + 1,
            )
        )
    return candidates, impression


def describe_summary(record: MaltegoLookupRecord) -> dict[str, dict]:
    """What a describe call says about each capability: inputs, outputs, settings."""
    summary = {}
    for item in (record.payload() or {}).get("items") or []:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            continue
        settings = (
            item.get("settings_schema") if isinstance(item.get("settings_schema"), dict) else {}
        )
        outputs = item.get("output_types") if isinstance(item.get("output_types"), dict) else {}
        summary[item["id"]] = {
            "name": item.get("name"),
            "description": item.get("description"),
            "input_types": item.get("input_types"),
            "output_types": outputs.get("effective") or outputs.get("declared"),
            "required_settings": settings.get("required") or [],
        }
    return summary


class Investigation:
    """One goal and subject, its lookups, its frozen records, and its outputs."""

    def __init__(self, folder: Path, manifest: dict):
        self.folder = folder
        self.manifest = manifest

    @classmethod
    def create(cls, root: Path, goal: str, entity_type: str, entity_value: str) -> Investigation:
        if not goal.strip() or not entity_type.strip() or not entity_value.strip():
            raise ValueError("An investigation needs a goal, an entity type, and an entity value.")
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        ignore = root / ".gitignore"
        if not ignore.exists():
            ignore.write_text(
                "# Investigations hold lookup data; keep them out of git.\n*\n", encoding="utf-8"
            )
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        folder = root / f"{stamp}-{slug(entity_value + ' ' + goal)}"
        folder.mkdir(mode=0o700)
        manifest = {
            "schema": SCHEMA,
            "goal": goal.strip(),
            "subject": {"entity_type": entity_type.strip(), "entity_value": entity_value.strip()},
            "created_at": now(),
            "investigator_version": INVESTIGATOR_VERSION,
            "plan": None,
            "lookups": [],
            "evidence_record_ids": [],
            "draft": None,
            "bundle": None,
            "checks": [],
            "log": [],
        }
        investigation = cls(folder, manifest)
        investigation.save()
        return investigation

    @classmethod
    def open(cls, folder: Path) -> Investigation:
        path = folder / MANIFEST
        if not path.is_file():
            raise FileNotFoundError(f"{folder} is not an investigation folder (no {MANIFEST}).")
        manifest = read_json(path)
        if manifest.get("schema") != SCHEMA:
            raise ValueError(f"{path} is not a {SCHEMA} manifest.")
        return cls(folder, manifest)

    @property
    def goal(self) -> str:
        return self.manifest["goal"]

    @property
    def subject(self) -> dict:
        return self.manifest["subject"]

    def save(self) -> None:
        write_json(self.folder / MANIFEST, self.manifest)

    def log(self, event: str, **details: Any) -> None:
        self.manifest["log"].append({"at": now(), "event": event, **details})
        self.save()

    # -- free discovery -------------------------------------------------------
    def plan(self, source: Any, *, max_results: int = 8, describe: int = 4) -> list[Candidate]:
        """Search (free) and describe the top candidates (free)."""
        search = source.search(
            self.goal,
            entity_type=self.subject["entity_type"],
            entity_value=self.subject["entity_value"],
            max_results=max_results,
        )
        candidates, impression = parse_candidates(search, self.subject["entity_type"])
        described: dict[str, dict] = {}
        describe_record = None
        chosen = [candidate.capability_id for candidate in candidates[:describe]]
        if chosen:
            describe_record = source.describe(chosen)
            described = describe_summary(describe_record)
        self.manifest["plan"] = {
            "at": now(),
            "search_record_id": search.record_id,
            "describe_record_id": describe_record.record_id if describe_record else None,
            "impression_id": impression,
            "candidates": [
                {**candidate.to_json(), "described": described.get(candidate.capability_id)}
                for candidate in candidates
            ],
        }
        self.log("plan", candidates=len(candidates), search_record_id=search.record_id)
        return candidates

    def validate(self, source: Any, capability_ids: Sequence[str]) -> list[dict]:
        """Ask the server to check entitlement and input without executing (free)."""
        results = []
        for capability_id in capability_ids:
            outcome = source.lookup(
                capability_id,
                entity_type=self.subject["entity_type"],
                entity_value=self.subject["entity_value"],
                validate_only=True,
                describe=False,
            )
            record = outcome.records[-1] if outcome.records else None
            results.append(
                {
                    "capability_id": capability_id,
                    "status": outcome.status,
                    "record_id": record.record_id if record else None,
                    "is_error": record.is_error if record else None,
                    "text": record.text[:600] if record else None,
                }
            )
        self.log(
            "validate",
            results=[{k: r[k] for k in ("capability_id", "status", "record_id")} for r in results],
        )
        return results

    # -- paid lookups -----------------------------------------------------------
    def lookup(
        self,
        source: Any,
        capability_ids: Sequence[str],
        *,
        max_results: int | None = 25,
        notify: Callable[[str], None] | None = None,
    ) -> list[dict]:
        """Run each named capability once on the subject; this spends credits."""
        plan = self.manifest.get("plan") or {}
        names = {item["capability_id"]: item.get("name") for item in plan.get("candidates") or []}
        entries = []
        for capability_id in capability_ids:
            outcome = source.lookup(
                capability_id,
                entity_type=self.subject["entity_type"],
                entity_value=self.subject["entity_value"],
                impression_id=plan.get("impression_id"),
                max_results=max_results,
                notify=notify,
            )
            entry = {
                "at": now(),
                "capability_id": capability_id,
                "capability_name": names.get(capability_id)
                or next(
                    (
                        r.context.get("capability_name")
                        for r in outcome.records
                        if r.context.get("capability_name")
                    ),
                    None,
                ),
                "status": outcome.status,
                "run_id": outcome.run_id,
                "idempotency_key": outcome.idempotency_key,
                "record_ids": [record.record_id for record in outcome.records],
                "evidence_record_ids": [record.record_id for record in outcome.evidence],
                "entities": outcome.entity_count,
                "credits": outcome.credits,
                "notes": list(outcome.notes),
            }
            entries.append(entry)
            self.manifest["lookups"].append(entry)
            self.add_evidence(entry["evidence_record_ids"])
            self.log(
                "lookup",
                capability_id=capability_id,
                status=outcome.status,
                credits=outcome.credits,
            )
        return entries

    def add_evidence(self, record_ids: Iterable[str]) -> None:
        current = self.manifest["evidence_record_ids"]
        for record_id in record_ids:
            if record_id not in current:
                current.append(record_id)
        self.save()

    def attach(self, store: RecordStore, record_ids: Sequence[str]) -> list[MaltegoLookupRecord]:
        """Use records already frozen in this workspace (no new lookup, no credits)."""
        records = [store.load(record_id) for record_id in record_ids]
        not_evidence = [r.record_id[:12] for r in records if r.evidence_kind != "entities"]
        if not_evidence:
            raise ValueError(
                "Only records with result entities can be evidence: " + ", ".join(not_evidence)
            )
        self.add_evidence(record.record_id for record in records)
        self.log("attach", record_ids=list(record_ids))
        return records

    # -- drafting -----------------------------------------------------------------
    def evidence_records(self, store: RecordStore) -> list[MaltegoLookupRecord]:
        records = [store.load(record_id) for record_id in self.manifest["evidence_record_ids"]]
        if not records:
            raise ValueError(
                "This investigation has no evidence records yet; run or attach a lookup."
            )
        return records

    def write(
        self,
        store: RecordStore,
        *,
        model: StructuredModel | None = None,
        draft: InvestigationDraft | None = None,
        draft_source: str = "model",
        draft_note: str | None = None,
        max_claims: int = 4,
    ) -> dict:
        """Draft claims (model or supplied draft) and build the evidence bundle."""
        records = self.evidence_records(store)
        configuration: dict[str, Any] = {"draft_source": draft_source}
        if draft_note:
            configuration["draft_note"] = draft_note.strip()
        if draft is None:
            if model is None:
                raise ValueError("Give a drafting model or a draft file.")
            preview = source_bundle(records, title=self.goal, question=self.goal)
            labels = record_labels(preview, records)
            by_id = {record["id"]: record for record in preview["records"]}
            shown = [(label, by_id[bundle_id]) for label, bundle_id in labels.items()]
            draft, messages = write_draft(
                model, self.goal, self.subject, shown, max_claims=max_claims
            )
            configuration.update(
                model=model.model,
                actual_model=getattr(model, "actual_model", None),
                provider=getattr(model, "provider", None),
                prompt_version=DRAFT_PROMPT_VERSION,
                prompt_sha256=digest(messages),
            )
        write_json(self.folder / "draft.json", draft.model_dump(mode="json"))
        built = build_investigation_bundle(
            records,
            draft,
            goal=self.goal,
            subject=self.subject,
            created_at=now(),
            configuration=configuration,
            coverage={"lookups": len(self.manifest["lookups"])},
        )
        write_json(self.folder / "bundle.json", built.bundle)
        write_json(
            self.folder / "citations.json",
            {"citations": built.citations, "problems": built.problems},
        )
        from evidence_explorer.review.contracts import EvidenceBundle

        fingerprint = EvidenceBundle.model_validate(built.bundle).fingerprint()
        self.manifest["draft"] = {
            "at": now(),
            **configuration,
            "claims": len(draft.claims),
            "assumptions": len(draft.assumptions),
            "citations": len(built.citations),
            "rejected_citations": sum(not item["accepted"] for item in built.citations),
            "problems": built.problems,
        }
        self.manifest["bundle"] = {"path": "bundle.json", "fingerprint": fingerprint}
        self.log("write", fingerprint=fingerprint, claims=len(draft.claims))
        return self.manifest["draft"]
