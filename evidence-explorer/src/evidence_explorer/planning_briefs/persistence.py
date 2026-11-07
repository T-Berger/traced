"""Persist planning revisions through the shared workspace connection factory.

The monitoring_briefs migration component owns planning history and local adoption
state. Case binding reads review_cases in the same transaction to pin actual saved
evidence; it therefore depends on the review schema. Planning revisions stay out
of EvidenceBundle identity and never create collection jobs or finding decisions.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from ..persistence.migrations import migrate
from ..review.contracts import EvidenceBundle
from ..review.persistence.common import ConnectionFactory, VersionConflictError
from . import schema
from .models import (
    MAX_HISTORY_BYTES,
    MAX_REVISION_BYTES,
    BriefBinding,
    BriefConfirmation,
    BriefRevision,
    DraftFields,
    PlanningHistory,
    canonical_json,
    digest,
    revision_content_hash,
)
from .scope import scope_identity, validate_history


@dataclass(frozen=True)
class StoredState:
    revision: BriefRevision
    head_revision_id: str | None
    local: bool
    confirmation: BriefConfirmation | None
    confirmation_is_local: bool
    binding_current: bool
    binding_problem: str | None = None
    is_active_for_case: bool = False


def _bounded(limit: int, offset: int) -> None:
    if isinstance(limit, bool) or not 1 <= limit <= 1000 or offset < 0:
        raise ValueError("Planning pages require a limit from 1 to 1000 and nonnegative offset.")


class PlanningBriefRepository:
    """Use caller-owned transactions for immutable revisions and guarded head changes.

    The injected connection factory may join a captured read snapshot or an atomic
    evidence/package import. This repository does not open a separate database;
    initialize() explicitly applies its independent migration component.
    """

    def __init__(
        self,
        connect: ConnectionFactory,
        *,
        clock: Callable[[], datetime] | None = None,
    ):
        self._connect = connect
        self._clock = clock or (lambda: datetime.now(UTC))

    def initialize(self) -> None:
        with self._connect() as connection:
            migrate(connection, schema.COMPONENT, schema.MIGRATIONS)

    @staticmethod
    def _head(connection: sqlite3.Connection, brief_id: str) -> str | None:
        row = connection.execute(
            "SELECT head_revision_id FROM monitoring_brief_heads WHERE brief_id=?", (brief_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"Unknown planning brief: {brief_id}")
        return row["head_revision_id"]

    @classmethod
    def _check_head(
        cls, connection: sqlite3.Connection, brief_id: str, expected: str | None
    ) -> str | None:
        current = cls._head(connection, brief_id)
        if current != expected:
            raise VersionConflictError("This planning brief changed. Reload before saving.")
        return current

    @staticmethod
    def _revision(connection: sqlite3.Connection, revision_id: str) -> BriefRevision:
        row = connection.execute(
            "SELECT revision_json FROM monitoring_brief_revisions WHERE revision_id=?",
            (revision_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"Unknown planning revision: {revision_id}")
        return BriefRevision.model_validate_json(row["revision_json"])

    @staticmethod
    def _case_binding(
        connection: sqlite3.Connection, case_id: str, expected_fingerprint: str | None = None
    ) -> BriefBinding:
        row = connection.execute(
            "SELECT fingerprint,bundle_json FROM review_cases WHERE id=?", (case_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"Unknown saved case: {case_id}")
        if expected_fingerprint is not None and row["fingerprint"] != expected_fingerprint:
            raise VersionConflictError(
                "The saved case changed. Review its scope before continuing."
            )
        bundle = EvidenceBundle.model_validate_json(row["bundle_json"])
        if bundle.case.id != case_id or bundle.fingerprint() != row["fingerprint"]:
            raise ValueError("Saved case identity or fingerprint is inconsistent.")
        return BriefBinding(
            case_id=case_id,
            source_case_fingerprint=row["fingerprint"],
            scope=scope_identity(bundle),
        )

    def case_binding(self, case_id: str) -> BriefBinding:
        with self._connect() as connection:
            return self._case_binding(connection, case_id)

    def _append(
        self,
        connection: sqlite3.Connection,
        brief_id: str,
        fields: DraftFields,
        binding: BriefBinding | None,
        author: str,
        parent: BriefRevision | None,
    ) -> BriefRevision:
        latest_number = connection.execute(
            "SELECT COALESCE(MAX(number),0) FROM monitoring_brief_revisions WHERE brief_id=?",
            (brief_id,),
        ).fetchone()[0]
        revision = BriefRevision(
            brief_id=brief_id,
            revision_id=str(uuid4()),
            number=latest_number + 1,
            parent_revision_id=parent.revision_id if parent else None,
            fields=fields,
            binding=binding,
            content_hash=revision_content_hash(fields, binding),
            author=author,
            created_at=self._clock(),
        )
        connection.execute(
            "INSERT INTO monitoring_brief_revisions "
            "(revision_id,brief_id,number,parent_revision_id,case_id,revision_json,local) "
            "VALUES (?,?,?,?,?,?,1)",
            (
                revision.revision_id,
                brief_id,
                revision.number,
                revision.parent_revision_id,
                binding.case_id if binding else None,
                canonical_json(revision.model_dump(mode="json")),
            ),
        )
        connection.execute(
            "UPDATE monitoring_brief_heads SET head_revision_id=? WHERE brief_id=?",
            (revision.revision_id, brief_id),
        )
        return revision

    def create(
        self, fields: DraftFields, *, author: str, brief_id: str | None = None
    ) -> BriefRevision:
        fields = DraftFields.model_validate(fields)
        brief_id = brief_id if brief_id is not None else str(uuid4())
        with self._connect(write=True) as connection:
            if connection.execute(
                "SELECT 1 FROM monitoring_brief_heads WHERE brief_id=?", (brief_id,)
            ).fetchone():
                raise ValueError("This planning brief identity already exists.")
            connection.execute(
                "INSERT INTO monitoring_brief_heads(brief_id) VALUES (?)", (brief_id,)
            )
            return self._append(connection, brief_id, fields, None, author, None)

    def save(
        self,
        brief_id: str,
        fields: DraftFields,
        *,
        base_revision_id: str,
        expected_head_revision_id: str,
        author: str,
    ) -> BriefRevision:
        fields = DraftFields.model_validate(fields)
        if base_revision_id != expected_head_revision_id:
            raise VersionConflictError("A historical revision requires explicit adoption.")
        with self._connect(write=True) as connection:
            self._check_head(connection, brief_id, expected_head_revision_id)
            parent = self._revision(connection, base_revision_id)
            if parent.brief_id != brief_id:
                raise ValueError("The base revision belongs to another planning brief.")
            if parent.binding is None:
                if self._lineage_cases(connection, brief_id):
                    raise VersionConflictError(
                        "Adopt a bound revision from this brief's history before saving. "
                        "To keep an unbound draft, copy your fields into a new brief."
                    )
            else:
                self._check_lineage_case(connection, brief_id, parent.binding.case_id)
            return self._append(connection, brief_id, fields, parent.binding, author, parent)

    @staticmethod
    def _active(connection: sqlite3.Connection, case_id: str, brief_id: str) -> None:
        connection.execute(
            "INSERT INTO monitoring_brief_active_cases(case_id,brief_id) VALUES (?,?) "
            "ON CONFLICT(case_id) DO UPDATE SET brief_id=excluded.brief_id",
            (case_id, brief_id),
        )

    @staticmethod
    def _lineage_cases(connection: sqlite3.Connection, brief_id: str) -> set[str]:
        return {
            row[0]
            for row in connection.execute(
                "SELECT DISTINCT case_id FROM monitoring_brief_revisions "
                "WHERE brief_id=? AND case_id IS NOT NULL LIMIT 2",
                (brief_id,),
            )
        }

    @classmethod
    def _check_lineage_case(
        cls, connection: sqlite3.Connection, brief_id: str, case_id: str
    ) -> None:
        if cls._lineage_cases(connection, brief_id) - {case_id}:
            raise ValueError(
                "This brief's history belongs to another case. Copy its fields to a new brief."
            )

    def bind(
        self,
        brief_id: str,
        case_id: str,
        *,
        expected_head_revision_id: str,
        expected_case_fingerprint: str,
        author: str,
    ) -> BriefRevision:
        with self._connect(write=True) as connection:
            self._check_head(connection, brief_id, expected_head_revision_id)
            parent = self._revision(connection, expected_head_revision_id)
            self._check_lineage_case(connection, brief_id, case_id)
            binding = self._case_binding(connection, case_id, expected_case_fingerprint)
            if set(parent.fields.source_selection) != set(binding.scope.source_refs):
                raise ValueError("Choose exactly the source references in the saved case scope.")
            revision = self._append(connection, brief_id, parent.fields, binding, author, parent)
            self._active(connection, case_id, brief_id)
            return revision

    def confirm(
        self,
        brief_id: str,
        *,
        expected_head_revision_id: str,
        expected_case_fingerprint: str,
        reviewer: str,
        validate: Callable[[BriefRevision], None],
    ) -> BriefConfirmation:
        with self._connect(write=True) as connection:
            self._check_head(connection, brief_id, expected_head_revision_id)
            revision = self._revision(connection, expected_head_revision_id)
            if revision.binding is None:
                raise ValueError("Bind this draft to a saved case before confirming it.")
            if not connection.execute(
                "SELECT 1 FROM monitoring_brief_active_cases WHERE case_id=? AND brief_id=?",
                (revision.binding.case_id, brief_id),
            ).fetchone():
                raise VersionConflictError(
                    "This case has another active plan. Bind this plan explicitly."
                )
            current = self._case_binding(
                connection, revision.binding.case_id, expected_case_fingerprint
            )
            if current.scope != revision.binding.scope:
                raise VersionConflictError("The bound evidence changed. Bind and review it again.")
            validate(revision)
            previous = connection.execute(
                "SELECT confirmation_json FROM monitoring_brief_confirmations "
                "WHERE revision_id=? AND local=1",
                (revision.revision_id,),
            ).fetchone()
            if previous is not None:
                confirmation = BriefConfirmation.model_validate_json(previous["confirmation_json"])
                if confirmation.reviewer != reviewer:
                    raise ValueError("This exact revision already has a local confirmation.")
                return confirmation
            confirmation = BriefConfirmation(
                confirmation_id=str(uuid4()),
                brief_id=brief_id,
                revision_id=revision.revision_id,
                binding_digest=digest(revision.binding.model_dump(mode="json")),
                reviewer=reviewer,
                confirmed_at=self._clock(),
            )
            connection.execute(
                "INSERT INTO monitoring_brief_confirmations "
                "(confirmation_id,revision_id,confirmation_json,local) VALUES (?,?,?,1)",
                (
                    confirmation.confirmation_id,
                    revision.revision_id,
                    canonical_json(confirmation.model_dump(mode="json")),
                ),
            )
            return confirmation

    def _state(
        self,
        connection: sqlite3.Connection,
        brief_id: str,
        revision_id: str | None = None,
        *,
        check_binding: bool = True,
    ) -> StoredState:
        head = self._head(connection, brief_id)
        selected = revision_id or head
        if selected is None:
            selected = connection.execute(
                "SELECT revision_id FROM monitoring_brief_revisions WHERE brief_id=? "
                "ORDER BY number DESC LIMIT 1",
                (brief_id,),
            ).fetchone()[0]
        revision = self._revision(connection, selected)
        if revision.brief_id != brief_id:
            raise ValueError("The selected revision belongs to another planning brief.")
        local = connection.execute(
            "SELECT local FROM monitoring_brief_revisions WHERE revision_id=?", (selected,)
        ).fetchone()[0]
        confirmation_row = connection.execute(
            "SELECT confirmation_json,local FROM monitoring_brief_confirmations "
            "WHERE revision_id=? ORDER BY local DESC,confirmation_id LIMIT 1",
            (selected,),
        ).fetchone()
        current, problem = False, None
        if revision.binding:
            try:
                if check_binding:
                    current = (
                        self._case_binding(connection, revision.binding.case_id).scope
                        == revision.binding.scope
                    )
                    if not current:
                        problem = "The saved evidence or source scope changed after binding."
                else:
                    row = connection.execute(
                        "SELECT fingerprint FROM review_cases WHERE id=?",
                        (revision.binding.case_id,),
                    ).fetchone()
                    current = bool(row and row[0] == revision.binding.source_case_fingerprint)
                    if not current:
                        problem = "Open this brief to verify freshness against saved evidence."
            except (KeyError, ValueError):
                problem = "The bound saved case is missing or has invalid scope provenance."
        active = bool(
            revision.binding
            and connection.execute(
                "SELECT 1 FROM monitoring_brief_active_cases WHERE case_id=? AND brief_id=?",
                (revision.binding.case_id, brief_id),
            ).fetchone()
        )
        return StoredState(
            revision,
            head,
            bool(local),
            BriefConfirmation.model_validate_json(confirmation_row["confirmation_json"])
            if confirmation_row
            else None,
            bool(confirmation_row and confirmation_row["local"]),
            current,
            problem,
            active,
        )

    def load(self, brief_id: str, revision_id: str | None = None) -> StoredState:
        with self._connect() as connection:
            if not connection.in_transaction:
                connection.execute("BEGIN")
            return self._state(connection, brief_id, revision_id)

    def list(self, *, limit: int = 50, offset: int = 0) -> tuple[StoredState, ...]:
        _bounded(limit, offset)
        if limit > 50:
            raise ValueError("List at most 50 planning briefs per page.")
        with self._connect() as connection:
            if not connection.in_transaction:
                connection.execute("BEGIN")
            rows = connection.execute(
                "SELECT brief_id FROM monitoring_brief_heads ORDER BY brief_id LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
            return self._list_states(connection, rows)

    def list_for_case(
        self, case_id: str | None, *, limit: int = 50, offset: int = 0
    ) -> tuple[StoredState, ...]:
        """Filter current metadata before paging; opening a picker never activates a plan."""
        _bounded(limit, offset)
        if limit > 50:
            raise ValueError("List at most 50 planning briefs per page.")
        with self._connect() as connection:
            if not connection.in_transaction:
                connection.execute("BEGIN")
            rows = connection.execute(
                "SELECT h.brief_id FROM monitoring_brief_heads h "
                "JOIN monitoring_brief_revisions r ON r.revision_id=COALESCE("
                "h.head_revision_id,(SELECT latest.revision_id "
                "FROM monitoring_brief_revisions latest WHERE latest.brief_id=h.brief_id "
                "ORDER BY latest.number DESC LIMIT 1)) "
                "LEFT JOIN monitoring_brief_active_cases a "
                "ON a.case_id=? AND a.brief_id=h.brief_id "
                "WHERE r.case_id IS NULL OR r.case_id=? "
                "ORDER BY CASE WHEN a.brief_id IS NOT NULL THEN 0 "
                "WHEN r.case_id IS NOT NULL THEN 1 ELSE 2 END,h.brief_id LIMIT ? OFFSET ?",
                (case_id, case_id, limit, offset),
            ).fetchall()
            return self._list_states(connection, rows)

    def _list_states(
        self, connection: sqlite3.Connection, rows: list[sqlite3.Row]
    ) -> tuple[StoredState, ...]:
        states, total = [], 0
        for row in rows:
            state = self._state(connection, row[0], check_binding=False)
            total += len(canonical_json(state.revision.model_dump(mode="json")).encode("utf-8"))
            if total > MAX_HISTORY_BYTES:
                raise ValueError("Planning list exceeds its byte budget; request a smaller page.")
            states.append(state)
        return tuple(states)

    def state_for_case(self, case_id: str) -> StoredState | None:
        with self._connect() as connection:
            if not connection.in_transaction:
                connection.execute("BEGIN")
            row = connection.execute(
                "SELECT a.brief_id FROM monitoring_brief_active_cases a "
                "JOIN monitoring_brief_heads h ON h.brief_id=a.brief_id "
                "JOIN monitoring_brief_revisions r ON r.revision_id=h.head_revision_id "
                "WHERE a.case_id=? AND r.case_id=?",
                (case_id, case_id),
            ).fetchone()
            if row is None:
                return None
            state = self._state(connection, row[0])
            binding = state.revision.binding
            return state if binding and binding.case_id == case_id else None

    def _history(
        self, connection: sqlite3.Connection, brief_ids: tuple[str, ...], limit: int, offset: int
    ) -> PlanningHistory:
        if not brief_ids:
            return PlanningHistory()
        placeholders = ",".join("?" for _ in brief_ids)
        revisions, total = [], 0
        for row in connection.execute(
            "SELECT revision_json FROM monitoring_brief_revisions "
            f"WHERE brief_id IN ({placeholders}) ORDER BY brief_id,number LIMIT ? OFFSET ?",
            (*brief_ids, limit, offset),
        ):
            size = len(row[0].encode("utf-8"))
            total += size
            if size > MAX_REVISION_BYTES or total > MAX_HISTORY_BYTES - 1024:
                raise ValueError("Planning history exceeds its bounded export byte budget.")
            revisions.append(BriefRevision.model_validate_json(row[0]))
        ids = tuple(revision.revision_id for revision in revisions)
        confirmations = ()
        if ids:
            placeholders = ",".join("?" for _ in ids)
            confirmations = []
            for row in connection.execute(
                "SELECT confirmation_json FROM monitoring_brief_confirmations "
                f"WHERE revision_id IN ({placeholders}) ORDER BY confirmation_id",
                ids,
            ):
                total += len(row[0].encode("utf-8"))
                if total > MAX_HISTORY_BYTES - 1024 or len(confirmations) >= 1000:
                    raise ValueError("Planning history exceeds its bounded export byte budget.")
                confirmations.append(BriefConfirmation.model_validate_json(row[0]))
        history = PlanningHistory(revisions=tuple(revisions), confirmations=tuple(confirmations))
        if len(canonical_json(history.model_dump(mode="json")).encode("utf-8")) > MAX_HISTORY_BYTES:
            raise ValueError("Planning history exceeds its bounded export byte budget.")
        return history

    def history(self, brief_id: str, *, limit: int = 100, offset: int = 0) -> PlanningHistory:
        _bounded(limit, offset)
        with self._connect() as connection:
            if not connection.in_transaction:
                connection.execute("BEGIN")
            self._head(connection, brief_id)
            return self._history(connection, (brief_id,), limit, offset)

    def history_for_case(
        self, case_id: str, *, limit: int = 1000, offset: int = 0
    ) -> PlanningHistory:
        _bounded(limit, offset)
        with self._connect() as connection:
            if not connection.in_transaction:
                connection.execute("BEGIN")
            return self._complete_case_history(connection, case_id, limit, offset)

    def _complete_case_history(
        self, connection: sqlite3.Connection, case_id: str, limit: int, offset: int
    ) -> PlanningHistory:
        ids = tuple(
            row[0]
            for row in connection.execute(
                "SELECT DISTINCT brief_id FROM monitoring_brief_revisions WHERE case_id=? "
                "ORDER BY brief_id LIMIT 1001",
                (case_id,),
            )
        )
        if len(ids) > 1000 or offset:
            raise ValueError("A complete relevant planning history exceeds its export bounds.")
        if ids:
            placeholders = ",".join("?" for _ in ids)
            count = connection.execute(
                "SELECT COUNT(*) FROM monitoring_brief_revisions "
                f"WHERE brief_id IN ({placeholders})",
                ids,
            ).fetchone()[0]
            if count > limit:
                raise ValueError("A complete relevant planning history exceeds its revision limit.")
        return self._history(connection, ids, limit, offset)

    def context_token_for_case(self, case_id: str) -> str:
        with self._connect() as connection:
            if not connection.in_transaction:
                connection.execute("BEGIN")
            history = self._complete_case_history(connection, case_id, 1000, 0)
            active = connection.execute(
                "SELECT brief_id FROM monitoring_brief_active_cases WHERE case_id=?", (case_id,)
            ).fetchone()
            brief_ids = sorted({row.brief_id for row in history.revisions})
            heads = {brief_id: self._head(connection, brief_id) for brief_id in brief_ids}
            return digest(
                {
                    "case_id": case_id,
                    "active_brief_id": active[0] if active else None,
                    "heads": heads,
                    "history": history.model_dump(mode="json"),
                }
            )

    def import_history(self, history: PlanningHistory) -> None:
        history = PlanningHistory.model_validate(history)
        if len(canonical_json(history.model_dump(mode="json")).encode("utf-8")) > MAX_HISTORY_BYTES:
            raise ValueError("Planning history exceeds its bounded import byte budget.")
        with self._connect(write=True) as connection:
            incoming_cases: dict[str, set[str]] = {}
            for revision in history.revisions:
                cases = incoming_cases.setdefault(revision.brief_id, set())
                if revision.binding:
                    cases.add(revision.binding.case_id)
            for brief_id, cases in incoming_cases.items():
                if len(cases | self._lineage_cases(connection, brief_id)) > 1:
                    raise ValueError(
                        "This brief's history belongs to another case. "
                        "Copy its fields to a new brief before importing another case."
                    )
            existing = {}
            for revision in history.revisions:
                if revision.parent_revision_id:
                    row = connection.execute(
                        "SELECT revision_json FROM monitoring_brief_revisions WHERE revision_id=?",
                        (revision.parent_revision_id,),
                    ).fetchone()
                    if row:
                        parent = BriefRevision.model_validate_json(row[0])
                        existing[parent.revision_id] = parent
            validate_history(history, existing_revisions=existing)
            for revision in sorted(history.revisions, key=lambda row: (row.brief_id, row.number)):
                payload = canonical_json(revision.model_dump(mode="json"))
                prior = connection.execute(
                    "SELECT revision_json FROM monitoring_brief_revisions WHERE revision_id=?",
                    (revision.revision_id,),
                ).fetchone()
                if prior:
                    if prior[0] != payload:
                        raise ValueError("A planning revision ID already has different content.")
                    continue
                if connection.execute(
                    "SELECT 1 FROM monitoring_brief_revisions WHERE brief_id=? AND number=?",
                    (revision.brief_id, revision.number),
                ).fetchone():
                    raise ValueError("A planning lineage number already has different content.")
                connection.execute(
                    "INSERT OR IGNORE INTO monitoring_brief_heads(brief_id) VALUES (?)",
                    (revision.brief_id,),
                )
                connection.execute(
                    "INSERT INTO monitoring_brief_revisions "
                    "(revision_id,brief_id,number,parent_revision_id,case_id,revision_json,local) "
                    "VALUES (?,?,?,?,?,?,0)",
                    (
                        revision.revision_id,
                        revision.brief_id,
                        revision.number,
                        revision.parent_revision_id,
                        revision.binding.case_id if revision.binding else None,
                        payload,
                    ),
                )
            for confirmation in history.confirmations:
                payload = canonical_json(confirmation.model_dump(mode="json"))
                prior = connection.execute(
                    "SELECT confirmation_json FROM monitoring_brief_confirmations "
                    "WHERE confirmation_id=?",
                    (confirmation.confirmation_id,),
                ).fetchone()
                if prior:
                    if prior[0] != payload:
                        raise ValueError(
                            "A planning confirmation ID already has different content."
                        )
                    continue
                connection.execute(
                    "INSERT INTO monitoring_brief_confirmations "
                    "(confirmation_id,revision_id,confirmation_json,local) VALUES (?,?,?,0)",
                    (confirmation.confirmation_id, confirmation.revision_id, payload),
                )

    def adopt(
        self,
        revision_id: str,
        case_id: str,
        *,
        expected_head_revision_id: str | None,
        expected_case_fingerprint: str,
        author: str,
    ) -> BriefRevision:
        with self._connect(write=True) as connection:
            source = self._revision(connection, revision_id)
            self._check_head(connection, source.brief_id, expected_head_revision_id)
            self._check_lineage_case(connection, source.brief_id, case_id)
            binding = self._case_binding(connection, case_id, expected_case_fingerprint)
            if set(source.fields.source_selection) != set(binding.scope.source_refs):
                raise ValueError("Choose exactly the source references in the saved case scope.")
            parent = (
                self._revision(connection, expected_head_revision_id)
                if expected_head_revision_id
                else source
            )
            revision = self._append(
                connection, source.brief_id, source.fields, binding, author, parent
            )
            self._active(connection, case_id, source.brief_id)
            return revision
