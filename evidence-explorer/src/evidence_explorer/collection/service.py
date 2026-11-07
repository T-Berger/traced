"""Configure collection and project saved items into shared-review evidence.

Service construction initializes collection tables. Commands register sources or
enqueue work; provider calls run in ``CollectionRuntime``. Case previews and bundle
builders read saved observations and use the shared source adapter without importing
a case or creating a human decision.
"""

from __future__ import annotations

import hashlib

from pydantic import ValidationError

from ..source_bundles import build_source_bundle
from .catalog import catalog
from .models import CollectionFailure, CollectionSource
from .ownership import canonical_key, find_feed_owner
from .persistence import CollectionRepository
from .registry import normalize_locator


class CollectionService:
    """Coordinate local collection commands, readiness, and saved-case projection.

    Readiness describes configured access, not a successful network fetch or
    historical completeness. Saved-feed ownership checks keep an existing source
    manager authoritative across the two scheduling interfaces.
    """

    def __init__(self, repository: CollectionRepository):
        self.repository = repository
        self.repository.initialize()

    def catalog(self) -> list[dict]:
        presets = catalog()
        saved = {
            canonical_key(source["platform"], source["locator"]): source
            for source in self.sources()
        }
        for preset in presets:
            source = saved.get(canonical_key(preset["platform"], preset["locator"]))
            preset["live_verified"] = bool(source and source["last_collected_at"])
            if source and source.get("managed_by") == "saved_feed":
                preset.update(ready=False, status=source["connection_status"])
            elif source and source["last_status"] == "failed":
                preset["status"] = source["last_error"] or "The last collection failed."
            elif source and source["last_collected_at"]:
                preset["status"] = (
                    f"Live fetch verified · {source['item_count']} saved records · "
                    f"{source['last_collected_at']}"
                )
        return presets

    def add(self, payload: dict) -> str:
        platform, locator = payload.get("platform"), payload.get("locator")
        if not isinstance(platform, str) or not isinstance(locator, str):
            raise CollectionFailure("Choose a platform and enter a channel or source URL.")
        locator = normalize_locator(platform, locator)
        identity = hashlib.sha256((platform + ":" + locator).encode()).hexdigest()[:24]
        try:
            source = CollectionSource.model_validate(
                {**payload, "id": "live-" + identity, "locator": locator}
            )
        except ValidationError:
            raise CollectionFailure(
                "Check the source name, interval (5–10,080 minutes), and item limit (1–20)."
            ) from None
        if len(self.repository.sources()) >= 100 and not any(
            canonical_key(row["platform"], row["locator"])
            == canonical_key(source.platform, source.locator)
            for row in self.repository.sources()
        ):
            raise CollectionFailure("This workspace already has 100 saved live sources.")
        return self.repository.add(source)

    @staticmethod
    def readiness(platform: str) -> dict:
        if platform == "supabase":
            from .supabase import configuration_status

            return configuration_status()
        if platform in {"x", "reddit"}:
            from .apis import configuration_status

            return configuration_status(platform)
        return {"ready": True, "message": "Public collection available"}

    def sources(self) -> list[dict]:
        return [self._source_access(source) for source in self.repository.sources()]

    def _source_access(self, source: dict) -> dict:
        managed = find_feed_owner(self.repository.db_path, source["platform"], source["locator"])
        status = self.readiness(source["platform"])
        return {
            **source,
            "ready": False if managed else status["ready"],
            "managed_by": "saved_feed" if managed else "collection",
            "connection_status": (
                "Managed in Saved feeds; its existing schedule collects this source."
                if managed
                else status["message"]
            ),
        }

    def _scope_snapshot(self, source_ids: list[str]) -> dict:
        snapshot = self.repository.scope_snapshot(source_ids)
        # Keep only safe configuration presence and our static explanation;
        # credential values never enter evidence or immutable case metadata.
        snapshot["sources"] = [self._source_access(source) for source in snapshot["sources"]]
        return snapshot

    def enqueue(self, source_id: str) -> str:
        source = self.repository.source(source_id)
        status = self.readiness(source.platform)
        if not status["ready"]:
            raise CollectionFailure(status["message"])
        return self.repository.enqueue(source_id)

    def enable(self, source_id: str, enabled: bool) -> None:
        if not isinstance(enabled, bool):
            raise CollectionFailure("Choose whether scheduled collection is enabled.")
        source = self.repository.source(source_id)
        if enabled and find_feed_owner(self.repository.db_path, source.platform, source.locator):
            raise CollectionFailure("This source's schedule is managed in Saved feeds.")
        self.repository.set_enabled(source_id, enabled)

    def status(self) -> dict:
        return {"sources": self.sources(), "jobs": self.repository.jobs()}

    def preview_case(self, source_ids: list[str]) -> dict:
        """Describe a selected saved-source snapshot without creating a case."""
        from .scoping import preview_scope

        return preview_scope(self._scope_snapshot(source_ids))

    def scoped_bundle(self, source_ids: list[str], *, expected_fingerprint: str):
        """Project saved observations only if they still match the caller's preview.

        The returned bundle is detached evidence for the workspace importer;
        building it neither fetches sources nor persists a review case.
        """
        from .scoping import preview_scope, scoped_bundle

        if not isinstance(expected_fingerprint, str) or not expected_fingerprint:
            raise CollectionFailure("Preview the selected sources before creating a case.")
        snapshot = self._scope_snapshot(source_ids)
        if preview_scope(snapshot)["fingerprint"] != expected_fingerprint:
            raise CollectionFailure(
                "The selected sources or saved evidence changed. Preview the selection again."
            )
        return scoped_bundle(snapshot)

    def bundle(self):
        """Build the legacy all-saved-items projection, or return None when empty.

        This compatibility path has no selected-source preview guard. New selected
        source flows use ``preview_case`` and ``scoped_bundle`` instead.
        """
        items = self.repository.items()
        if not items:
            return None
        return build_source_bundle(
            {
                "source_schema": "1.0",
                "case": {
                    "title": "Live multi-platform collection",
                    "question": (
                        "What do the collected public records establish across these sources?"
                    ),
                    "synthetic": False,
                },
                "items": items,
            }
        )
