"""Guard category writes against exceeding the existing native package limit."""

from __future__ import annotations

import json

from ..review.integrations import packages
from ..review.integrations.snapshots import _SnapshotRepository
from ..review.integrations.workspace import WorkspaceService


def ensure_transaction_bundle_fits(repository, connection, settings, case_id: str) -> None:
    """Check the eventual exported bundle, including local review history, before commit.

    The connection belongs to the caller's existing write transaction. This adds
    no writes and deliberately does not change the package format or its limit.
    """
    readonly = _SnapshotRepository(repository, connection)
    snapshot = object.__new__(WorkspaceService)
    snapshot.repository = readonly
    snapshot.settings = settings
    snapshot.artifact_root = settings.data_dir / "trail-artifacts"
    try:
        bundle = snapshot.export_bundle(case_id)
        payload = json.dumps(
            bundle.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, indent=2
        ).encode("utf-8")
    finally:
        readonly._snapshot_active = False
    if len(payload) > packages.MAX_FILE_BYTES:
        raise ValueError(
            "This result would make the case too large for the existing case-package limit. "
            "No proposals were saved; use a smaller case or an independently saved subset."
        )
