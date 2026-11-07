"""Maltego Data MCP operations shared by the command line and the review desk.

Every operation is an explicit action. Nothing here schedules lookups: Data MCP
runs spend shared team credits and some take minutes. Sign-in status reads only
the local token file; network access happens in `check`, `tools`, `run`,
`search`, `describe`, and `lookup`.

Each Data MCP call is frozen as its own hashed record, so a lookup that
describes a capability, starts a run, and polls for results leaves one
verifiable record per call, linked by the capability, input, and run ID that
Traced records as acquisition context.
"""

from __future__ import annotations

import re
import time
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from ..models import CollectionFailure
from .oauth import (
    SignInResult,
    SignInStatus,
    SignOutResult,
    TokenProvider,
    sign_in,
    sign_in_status,
    sign_out,
)
from .records import MaltegoLookupRecord, RecordStore, build_record
from .settings import MaltegoSettings, maltego_settings
from .tokens import TokenStore
from .transport import McpSession, ServerIdentity, ToolCall, ToolDescriptor

# Catalog reloads are slow, shared, and start by themselves when a catalog is stale.
BLOCKED_TOOLS = {
    "maltego_refresh_catalog": (
        "Traced does not start Maltego catalog reloads: they are slow and shared, and a stale "
        "catalog refreshes by itself. Use maltego_catalog_status to see its state."
    )
}
RUNNING_STATES = frozenset({"running", "queued", "pending", "submitted", "started"})
MAX_RESULT_PAGES = 10
STATUS_PAGE_LIMIT = 100

_CAPABILITY_ID = re.compile(r"(ci|mx)_[A-Za-z0-9_-]{1,120}")
_CREDENTIAL_KEY = re.compile(
    r"api[-_ ]?key|secret|passw(or)?d|token|credential|authorization|bearer", re.IGNORECASE
)


@dataclass(frozen=True)
class ConnectionCheck:
    server: ServerIdentity
    tools: tuple[ToolDescriptor, ...]


@dataclass(frozen=True)
class LookupOutcome:
    """Every record a lookup froze, in call order, and where the run ended."""

    records: tuple[MaltegoLookupRecord, ...]
    status: str
    run_id: str | None
    idempotency_key: str
    notes: tuple[str, ...] = field(default=())

    @property
    def evidence(self) -> tuple[MaltegoLookupRecord, ...]:
        return tuple(record for record in self.records if record.evidence_kind == "entities")

    @property
    def entity_count(self) -> int:
        return sum(len((record.payload() or {}).get("entities") or []) for record in self.evidence)

    @property
    def credits(self) -> dict | None:
        for record in reversed(self.records):
            credits = (record.payload() or {}).get("credits")
            if isinstance(credits, dict):
                return credits
        return None


def refuse_credentials(value: object, path: str = "arguments") -> None:
    """Keep vendor keys and secrets out of frozen, shareable evidence records."""
    if isinstance(value, Mapping):
        for key, item in value.items():
            if isinstance(key, str) and _CREDENTIAL_KEY.search(key):
                raise CollectionFailure(
                    f"Traced will not store {path}.{key} in an evidence record. Configure vendor "
                    "keys in Maltego (Hub or Data Pass) instead of passing them to a lookup."
                )
            refuse_credentials(item, f"{path}.{key}")
    elif isinstance(value, list):
        for item in value:
            refuse_credentials(item, path)


def _running(payload: Mapping[str, Any]) -> bool:
    status = str(payload.get("status") or "").lower()
    run_status = str(payload.get("run_status") or "").lower()
    return status in RUNNING_STATES or run_status in RUNNING_STATES


def _entities(payload: Mapping[str, Any]) -> list:
    entities = payload.get("entities")
    return entities if isinstance(entities, list) else []


def _total(payload: Mapping[str, Any]) -> int | None:
    count = payload.get("count")
    return count if isinstance(count, int) and not isinstance(count, bool) else None


def _poll_delay(payload: Mapping[str, Any]) -> float:
    value = payload.get("poll_after_seconds")
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(min(max(value, 0.0), 30.0))
    return 2.0


def capability_name(record: MaltegoLookupRecord, capability_id: str) -> str | None:
    for item in (record.payload() or {}).get("items") or []:
        if isinstance(item, dict) and item.get("id") == capability_id:
            name = item.get("name")
            return name.strip()[:300] if isinstance(name, str) and name.strip() else None
    return None


class MaltegoDataSource:
    """Explicit Data MCP actions for one person's sign-in and one workspace."""

    def __init__(
        self,
        settings: MaltegoSettings | None = None,
        *,
        data_dir: Path | None = None,
        transport: httpx.BaseTransport | None = None,
        token_store: TokenStore | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.settings = settings or maltego_settings()
        self.token_store = token_store or TokenStore(self.settings.token_path)
        self.transport = transport
        self.records = RecordStore(data_dir) if data_dir is not None else None
        self.sleep = sleep

    def status(self) -> SignInStatus:
        return sign_in_status(self.settings, self.token_store)

    def sign_in(self, **options) -> SignInResult:
        return sign_in(self.settings, store=self.token_store, transport=self.transport, **options)

    def sign_out(self) -> SignOutResult:
        return sign_out(self.settings, store=self.token_store, transport=self.transport)

    def session(self) -> McpSession:
        provider = TokenProvider(self.settings, self.token_store, transport=self.transport)
        return McpSession(self.settings, provider, transport=self.transport)

    def check(self) -> ConnectionCheck:
        with self.session() as session:
            server = session.initialize()
            return ConnectionCheck(server, tuple(session.list_tools()))

    def tools(self) -> list[ToolDescriptor]:
        with self.session() as session:
            return session.list_tools()

    def _freeze(
        self, call: ToolCall, context: Mapping[str, Any] | None = None, *, save: bool = True
    ) -> MaltegoLookupRecord:
        record = build_record(call, self.settings.server_url, context)
        if save and self.records is not None:
            self.records.save(record)
        return record

    def _call(
        self,
        session: McpSession,
        tool_name: str,
        arguments: dict,
        context: Mapping[str, Any] | None = None,
    ) -> MaltegoLookupRecord:
        if tool_name in BLOCKED_TOOLS:
            raise CollectionFailure(BLOCKED_TOOLS[tool_name])
        refuse_credentials(arguments)
        return self._freeze(session.call_tool(tool_name, arguments), context)

    def run(
        self, tool_name: str, arguments: dict, *, context: Mapping[str, Any] | None = None
    ) -> MaltegoLookupRecord:
        """Call one tool and freeze the result; error results are frozen for audit too."""
        if tool_name in BLOCKED_TOOLS:
            raise CollectionFailure(BLOCKED_TOOLS[tool_name])
        refuse_credentials(arguments)
        with self.session() as session:
            return self._call(session, tool_name, arguments, context)

    def search(
        self,
        goal: str,
        *,
        entity_type: str | None = None,
        entity_value: str | None = None,
        entity_ref: str | None = None,
        query: str | None = None,
        kind: str | None = None,
        max_results: int | None = None,
    ) -> MaltegoLookupRecord:
        if not isinstance(goal, str) or not goal.strip():
            raise CollectionFailure("State the investigative question in one sentence.")
        arguments = {
            "goal": goal.strip(),
            "entity_type": entity_type,
            "entity_value": entity_value,
            "entity_ref": entity_ref,
            "query": query,
            "kind": kind,
            "max_results": max_results,
        }
        return self.run(
            "maltego_search", {key: value for key, value in arguments.items() if value is not None}
        )

    def describe(self, ids: Iterable[str]) -> MaltegoLookupRecord:
        chosen = list(dict.fromkeys(ids))
        if not 1 <= len(chosen) <= 20:
            raise CollectionFailure("Describe between 1 and 20 capability or entity type IDs.")
        return self.run("maltego_describe", {"ids": chosen})

    def lookup(
        self,
        capability_id: str,
        *,
        entity_type: str | None = None,
        entity_value: str | None = None,
        entity_ref: str | None = None,
        entity_properties: dict | None = None,
        settings: dict | None = None,
        max_results: int | None = None,
        impression_id: str | None = None,
        after_run_id: str | None = None,
        idempotency_key: str | None = None,
        describe: bool = True,
        wait: bool = True,
        validate_only: bool = False,
        notify: Callable[[str], None] | None = None,
    ) -> LookupOutcome:
        """Run one capability on one entity, waiting for and paging its results.

        With ``validate_only`` the server checks entitlement, input, and settings and
        executes nothing, so no credits are spent and no idempotency key is sent.
        """
        if not isinstance(capability_id, str) or not _CAPABILITY_ID.fullmatch(capability_id):
            raise CollectionFailure("Use a transform (ci_…) or multiplexer (mx_…) ID from search.")
        if entity_ref is None and not (entity_type and entity_value):
            raise CollectionFailure("Give an entity reference, or an entity type and value.")
        entity = {
            "entity_ref": entity_ref,
            "entity_type": entity_type,
            "entity_value": entity_value,
        }
        entity = {key: value for key, value in entity.items() if value is not None}
        key = "" if validate_only else idempotency_key or "traced-" + uuid.uuid4().hex
        arguments: dict[str, Any] = {
            "capability_id": capability_id,
            **entity,
            "entity_properties": entity_properties,
            "settings": settings,
            "max_results": max_results,
            "impression_id": impression_id,
            "after_run_id": after_run_id,
            "validate_only": True if validate_only else None,
            "idempotency_key": key or None,
        }
        arguments = {name: value for name, value in arguments.items() if value is not None}
        refuse_credentials(arguments)
        context: dict[str, Any] = {"capability_id": capability_id, "input": entity}
        say = notify or (lambda _message: None)
        records: list[MaltegoLookupRecord] = []
        notes: list[str] = []
        with self.session() as session:
            if describe:
                described = self._call(
                    session, "maltego_describe", {"ids": [capability_id]}, {"purpose": "describe"}
                )
                records.append(described)
                name = capability_name(described, capability_id)
                if name:
                    context["capability_name"] = name
            say("Running the capability; some sources take minutes.")
            started = self._call(session, "maltego_run", arguments, context)
            records.append(started)
            payload = started.payload() or {}
            run_id = payload.get("run_id") if isinstance(payload.get("run_id"), str) else None
            status = str(payload.get("status") or ("error" if started.is_error else "unknown"))
            if run_id and wait and (_running(payload) or self._more(payload, payload)):
                status, more_notes = self._collect(session, run_id, context, payload, records, say)
                notes.extend(more_notes)
        return LookupOutcome(tuple(records), status, run_id, key, tuple(notes))

    def wait_for_run(
        self, run_id: str, *, notify: Callable[[str], None] | None = None
    ) -> LookupOutcome:
        """Resume an earlier long run by ID and freeze its status and result pages."""
        if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", run_id):
            raise CollectionFailure("Use the run_id that maltego_run returned.")
        records: list[MaltegoLookupRecord] = []
        with self.session() as session:
            status, notes = self._collect(
                session, run_id, {"run_id": run_id}, {"status": "running"}, records, notify
            )
        return LookupOutcome(tuple(records), status, run_id, "", tuple(notes))

    @staticmethod
    def _more(payload: Mapping[str, Any], first: Mapping[str, Any]) -> bool:
        total = _total(payload)
        return total is not None and total > len(_entities(first))

    def _collect(
        self,
        session: McpSession,
        run_id: str,
        context: Mapping[str, Any],
        payload: Mapping[str, Any],
        records: list[MaltegoLookupRecord],
        notify: Callable[[str], None] | None,
    ) -> tuple[str, list[str]]:
        say = notify or (lambda _message: None)
        status_context = {**context, "run_id": run_id}
        deadline = time.monotonic() + self.settings.timeout_seconds
        notes: list[str] = []
        fetched = len(_entities(payload)) if not _running(payload) else 0
        while _running(payload):
            if time.monotonic() >= deadline:
                notes.append(
                    f"The run is still going after {int(self.settings.timeout_seconds)} s; resume "
                    f"it with './traced maltego wait {run_id}'."
                )
                return "running", notes
            record = self._call(
                session,
                "maltego_run_status",
                {"run_id": run_id, "wait_seconds": 20, "verbosity": "detailed"},
                status_context,
            )
            records.append(record)
            payload = record.payload() or {}
            if record.is_error or not payload:
                return "error", notes
            if _running(payload):
                say("Still running…")
                self.sleep(_poll_delay(payload))
            else:
                fetched = len(_entities(payload))
        status = str(payload.get("status") or payload.get("run_status") or "unknown")
        total = _total(payload)
        pages = 0
        while total is not None and fetched < total:
            if pages >= MAX_RESULT_PAGES:
                notes.append(
                    f"Saved {fetched} of {total} result entities; page limit reached, so the "
                    "remaining entities are not in these records."
                )
                break
            record = self._call(
                session,
                "maltego_run_status",
                {
                    "run_id": run_id,
                    "verbosity": "detailed",
                    "entities_offset": fetched,
                    "entities_limit": STATUS_PAGE_LIMIT,
                },
                status_context,
            )
            records.append(record)
            page = _entities(record.payload() or {})
            pages += 1
            if record.is_error or not page:
                notes.append(f"Saved {fetched} of {total} result entities; paging stopped early.")
                break
            fetched += len(page)
        return status, notes
