"""Planning travels beside native evidence without changing or discarding it."""

import base64
import hashlib
import io
import json
import struct
import zipfile
import zlib
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from evidence_explorer.config import Settings
from evidence_explorer.planning_briefs.models import (
    BriefBinding,
    BriefConfirmation,
    BriefRevision,
    DraftFields,
    PlanningHistory,
    digest,
    revision_content_hash,
)
from evidence_explorer.planning_briefs.scope import scope_identity
from evidence_explorer.review.contracts import EvidenceBundle
from evidence_explorer.review.demo import load_demo_bundle
from evidence_explorer.review.integrations import packages
from evidence_explorer.review.integrations.packages import (
    FORMAT,
    PLANNING_FILE,
    PLANNING_FORMAT,
    export_case_package,
    import_case_package,
    install_case_artifacts,
    read_case_package,
)
from evidence_explorer.review.integrations.web import create_app
from evidence_explorer.review.integrations.workspace import WorkspaceRepository, WorkspaceService
from evidence_explorer.review.workflow import evidence_fingerprint
from tests.integrations.test_handoff_flow import post_callback

STAMP = datetime(2026, 11, 7, 9, tzinfo=UTC)


@pytest.fixture
def bundle():
    text = "The saved bridge statement is unchanged."
    return EvidenceBundle.model_validate(
        {
            "case": {
                "id": "package:test",
                "title": "Package fixture",
                "question": "What does this source say?",
                "synthetic": True,
            },
            "records": [
                {
                    "id": "source:1",
                    "kind": "article",
                    "source_label": "Synthetic source",
                    "text": text,
                    "content_hash": hashlib.sha256(text.encode()).hexdigest(),
                }
            ],
            "fragments": [],
            "runs": [],
            "findings": [],
        }
    )


def planning_history(bundle):
    scope = scope_identity(bundle)
    fields = DraftFields(
        use_case_id="cited_claim_investigation",
        purpose="Check the supplied public bridge statement.",
        public_subjects_or_topics=("Bridge access",),
        source_selection=scope.source_refs,
        reviewer_role="Research reviewer",
        expected_output="reviewed_brief",
    )
    initial = BriefRevision(
        brief_id="brief:one",
        revision_id="revision:one",
        number=1,
        fields=DraftFields(),
        content_hash=revision_content_hash(DraftFields(), None),
        author="Planning author",
        created_at=STAMP,
    )
    binding = BriefBinding(
        case_id=bundle.case.id,
        source_case_fingerprint=bundle.fingerprint(),
        scope=scope,
    )
    bound = BriefRevision(
        brief_id=initial.brief_id,
        revision_id="revision:two",
        number=2,
        parent_revision_id=initial.revision_id,
        fields=fields,
        binding=binding,
        content_hash=revision_content_hash(fields, binding),
        author="Planning author",
        created_at=STAMP,
    )
    confirmation = BriefConfirmation(
        confirmation_id="confirmation:one",
        brief_id=bound.brief_id,
        revision_id=bound.revision_id,
        binding_digest=digest(binding.model_dump(mode="json")),
        reviewer="Planning reviewer",
        confirmed_at=STAMP,
    )
    return PlanningHistory(revisions=(initial, bound), confirmations=(confirmation,))


def media_case(bundle, tmp_path, *, reference="media/frame.svg", second=False):
    data = bundle.model_dump(mode="json")
    root = tmp_path / "original"
    references = [reference, "media/second.svg"] if second else [reference]
    for index, ref in enumerate(references):
        content = f"<svg><text>Synthetic frame {index}</text></svg>".encode()
        path = root / ref
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        data["records"].append(
            {
                "id": f"image:{index}",
                "kind": "image",
                "source_label": "Supplied synthetic frame",
                "artifact_ref": ref,
                "content_hash": hashlib.sha256(content).hexdigest(),
            }
        )
    return EvidenceBundle.model_validate(data), root


def members(data):
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def repack(files, *, rehash=True):
    files = dict(files)
    if rehash:
        manifest = json.loads(files["manifest.json"])
        manifest["files"] = {
            name: {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
            for name, data in files.items()
            if name != "manifest.json"
        }
        files["manifest.json"] = json.dumps(manifest).encode()
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return output.getvalue()


def corrupt_deflate_member(payload, member):
    """Keep the ZIP structure valid, but set its member's reserved DEFLATE block type."""
    damaged = bytearray(payload)
    with zipfile.ZipFile(io.BytesIO(damaged)) as archive:
        info = archive.getinfo(member)
        assert info.compress_type == zipfile.ZIP_DEFLATED
        name_size, extra_size = struct.unpack_from("<HH", damaged, info.header_offset + 26)
        offset = info.header_offset + 30 + name_size + extra_size
        damaged[offset] = (damaged[offset] & ~6) | 6
    return bytes(damaged)


@pytest.mark.parametrize(
    "with_planning,member",
    [
        (False, "manifest.json"),
        (False, "bundle.json"),
        (False, "media/frame.svg"),
        (True, "manifest.json"),
        (True, "bundle.json"),
        (True, PLANNING_FILE),
        (True, "media/frame.svg"),
    ],
)
def test_invalid_deflate_is_normalized_before_any_artifact_write(
    bundle, tmp_path, with_planning, member
):
    bundle, root = media_case(bundle, tmp_path)
    history = planning_history(bundle) if with_planning else None
    damaged = corrupt_deflate_member(
        export_case_package(bundle, root, planning_history=history), member
    )
    destination = tmp_path / "receiver"
    readers = [read_case_package] if with_planning else [read_case_package, import_case_package]
    for reader in readers:
        with pytest.raises(
            ValueError, match="compressed data is damaged.*valid case package"
        ) as exc:
            reader(damaged, destination)
        assert isinstance(exc.value.__cause__, zlib.error)
        assert "invalid block type" not in str(exc.value)
        assert "Error -3" not in str(exc.value)
        assert not destination.exists()


@pytest.mark.parametrize(
    "with_planning,member",
    [
        (False, "manifest.json"),
        (True, "manifest.json"),
        (True, "bundle.json"),
        (True, PLANNING_FILE),
    ],
)
def test_registered_upload_recovers_from_corrupt_deflate_without_saving(
    bundle, tmp_path, with_planning, member
):
    existing = bundle.model_copy(deep=True)
    existing.case.id = "receiver:existing"
    settings = Settings(data_dir=tmp_path / "desk")
    repository = WorkspaceRepository(settings.db_path)
    repository.initialize()
    service = WorkspaceService(repository, settings, settings.data_dir / "artifacts")
    service.import_bundle(existing.model_dump(mode="json"))
    service.planning_briefs.create_draft(author="Existing draft author")
    cases_before = service.list_cases()
    planning_before = service.planning_briefs.list()
    bundle, source = media_case(bundle, tmp_path)
    payload = export_case_package(
        bundle, source, planning_history=planning_history(bundle) if with_planning else None
    )
    damaged = corrupt_deflate_member(payload, member)
    app = create_app(service)
    response = post_callback(
        app,
        "trail-import-message.children",
        "trail-upload",
        {"trail-upload": "data:application/zip;base64," + base64.b64encode(damaged).decode()},
    )
    notice = json.dumps(response["trail-import-message"])
    assert "Invalid evidence package" in notice and "rv-notice--error" in notice
    assert "compressed data is damaged" in notice and "Choose a valid case package" in notice
    assert "invalid block type" not in notice and "Error -3" not in notice
    assert "rv-case" not in response  # Dash no_update preserves the existing selection.
    assert service.list_cases() == cases_before
    assert service.planning_briefs.list() == planning_before
    assert not service.artifact_root.exists()
    with repository._connection() as connection:
        for table in ("review_events", "review_jobs", "collection_jobs"):
            assert connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0

    # A failed decode does not poison a later valid upload through the same callback.
    recovered = post_callback(
        app,
        "trail-import-message.children",
        "trail-upload",
        {"trail-upload": "data:application/zip;base64," + base64.b64encode(payload).decode()},
    )
    assert recovered["rv-case"]["value"] == bundle.case.id
    assert service.repository.get_bundle(bundle.case.id) == bundle
    assert (service.artifact_root / "media/frame.svg").read_bytes() == (
        source / "media/frame.svg"
    ).read_bytes()


def test_plain_v1_bytes_and_reader_behavior_remain_compatible(bundle, tmp_path):
    payload = export_case_package(bundle)
    # This digest is the deterministic pre-sidecar serializer's minimal fixture.
    assert hashlib.sha256(payload).hexdigest() == (
        "c4f2e5632196f218a2948de1e7bfbe16ab5622d5713cadd52fcd574311e85db9"
    )
    parsed = read_case_package(payload, tmp_path)
    assert parsed.manifest.format == FORMAT
    assert parsed.planning_history is None
    assert parsed.artifacts == parsed.created_artifacts == ()
    assert import_case_package(payload, tmp_path) == bundle


def test_v2_roundtrip_keeps_native_bundle_and_exact_planning_history(bundle, tmp_path):
    history = planning_history(bundle)
    before = bundle.model_dump_json()
    plain = members(export_case_package(bundle))
    payload = export_case_package(bundle, planning_history=history)
    files = members(payload)
    assert files["bundle.json"] == plain["bundle.json"]
    assert set(files) == {"manifest.json", "bundle.json", PLANNING_FILE}
    parsed = read_case_package(payload, tmp_path)
    assert parsed.manifest.format == PLANNING_FORMAT
    assert parsed.planning_history == history
    assert parsed.bundle.model_dump_json() == before == bundle.model_dump_json()
    assert parsed.bundle.fingerprint() == bundle.fingerprint()
    assert evidence_fingerprint(parsed.bundle.model_dump(mode="json")) == evidence_fingerprint(
        bundle.model_dump(mode="json")
    )
    assert export_case_package(parsed.bundle, planning_history=parsed.planning_history) == payload


def test_legacy_reader_rejects_v2_before_any_artifact_write(bundle, tmp_path):
    bundle, root = media_case(bundle, tmp_path)
    payload = export_case_package(bundle, root, planning_history=planning_history(bundle))
    destination = tmp_path / "receiver"
    with pytest.raises(ValueError, match="v2.*planning history.*read_case_package"):
        import_case_package(payload, destination)
    assert not destination.exists()


def test_staged_read_and_install_support_atomic_case_persistence(bundle, tmp_path):
    bundle, root = media_case(bundle, tmp_path)
    payload = export_case_package(bundle, root, planning_history=planning_history(bundle))
    destination = tmp_path / "receiver"
    parsed = read_case_package(payload, destination, install_artifacts=False)
    assert not destination.exists()
    assert parsed.created_artifacts == ()
    created = install_case_artifacts(parsed, destination)
    assert created == (destination / "media/frame.svg",)
    assert created[0].read_bytes() == (root / "media/frame.svg").read_bytes()
    assert install_case_artifacts(parsed, destination) == ()


def test_default_reader_reports_only_newly_created_artifacts(bundle, tmp_path):
    bundle, root = media_case(bundle, tmp_path)
    payload = export_case_package(bundle, root, planning_history=planning_history(bundle))
    destination = tmp_path / "receiver"
    first = read_case_package(payload, destination)
    assert first.created_artifacts == (destination / "media/frame.svg",)
    assert read_case_package(payload, destination).created_artifacts == ()


@pytest.mark.parametrize("rehash", [False, True])
def test_corrupt_sidecar_never_writes_media_even_with_updated_manifest(bundle, tmp_path, rehash):
    bundle, root = media_case(bundle, tmp_path)
    files = members(export_case_package(bundle, root, planning_history=planning_history(bundle)))
    altered = json.loads(files[PLANNING_FILE])
    altered["revisions"][1]["fields"]["purpose"] = "Tampered planning purpose."
    files[PLANNING_FILE] = json.dumps(altered).encode()
    destination = tmp_path / "receiver"
    with pytest.raises(ValueError):
        read_case_package(repack(files, rehash=rehash), destination)
    assert not destination.exists()


@pytest.mark.parametrize("change", ["version", "parent", "confirmation", "duplicate", "empty"])
def test_invalid_history_fails_before_artifact_writes(bundle, tmp_path, change):
    bundle, root = media_case(bundle, tmp_path)
    files = members(export_case_package(bundle, root, planning_history=planning_history(bundle)))
    altered = json.loads(files[PLANNING_FILE])
    if change == "version":
        altered["schema_version"] = 2
    elif change == "parent":
        altered["revisions"] = altered["revisions"][1:]
    elif change == "confirmation":
        altered["confirmations"][0]["binding_digest"] = "0" * 64
    elif change == "duplicate":
        altered["revisions"].append(altered["revisions"][0])
    else:
        altered["revisions"] = altered["confirmations"] = []
    files[PLANNING_FILE] = json.dumps(altered).encode()
    destination = tmp_path / "receiver"
    with pytest.raises(ValueError):
        read_case_package(repack(files), destination)
    assert not destination.exists()


def test_other_case_history_is_rejected_on_export_and_import(bundle, tmp_path):
    other = bundle.model_copy(deep=True)
    other.case.id = "package:other"
    wrong_history = planning_history(other)
    with pytest.raises(ValueError, match="another case"):
        export_case_package(bundle, planning_history=wrong_history)
    bundle, root = media_case(bundle, tmp_path)
    files = members(export_case_package(bundle, root, planning_history=planning_history(bundle)))
    files[PLANNING_FILE] = wrong_history.model_dump_json().encode()
    destination = tmp_path / "receiver"
    with pytest.raises(ValueError, match="another case"):
        read_case_package(repack(files), destination)
    assert not destination.exists()


def test_unrelated_unbound_branch_is_not_exported_with_case_history(bundle):
    history = planning_history(bundle)
    initial = history.revisions[0]
    extra = initial.model_copy(
        update={
            "revision_id": "unrelated:draft",
            "number": 3,
            "parent_revision_id": initial.revision_id,
        }
    )
    unrelated = history.model_copy(update={"revisions": (*history.revisions, extra)})
    with pytest.raises(ValueError, match="unrelated|ancestor"):
        export_case_package(bundle, planning_history=unrelated)


def test_historical_binding_keeps_its_original_evidence_and_source_fingerprint(bundle, tmp_path):
    history = planning_history(bundle)
    old = history.revisions[1].binding
    changed = bundle.model_copy(deep=True)
    changed.records[0].metadata["later_annotation"] = "New saved evidence metadata"
    assert scope_identity(changed).evidence_fingerprint != old.scope.evidence_fingerprint
    parsed = read_case_package(export_case_package(changed, planning_history=history), tmp_path)
    assert parsed.planning_history.revisions[1].binding == old
    assert parsed.planning_history.confirmations == history.confirmations


def test_historical_scope_hash_corruption_is_rejected_even_after_other_hashes_are_updated(bundle):
    history = planning_history(bundle)
    fields = history.revisions[1].fields
    binding = history.revisions[1].binding
    scope = binding.scope.model_copy(update={"scope_fingerprint": "0" * 64})
    binding = binding.model_copy(update={"scope": scope})
    revision = history.revisions[1].model_copy(
        update={"binding": binding, "content_hash": revision_content_hash(fields, binding)}
    )
    confirmation = history.confirmations[0].model_copy(
        update={"binding_digest": digest(binding.model_dump(mode="json"))}
    )
    corrupt = history.model_copy(
        update={"revisions": (history.revisions[0], revision), "confirmations": (confirmation,)}
    )
    changed = bundle.model_copy(deep=True)
    changed.records[0].metadata["new"] = "A different current scope"
    with pytest.raises(ValueError, match="scope.*hash|scope.*fingerprint"):
        export_case_package(changed, planning_history=corrupt)


@pytest.mark.parametrize("change", ["missing", "extra_path", "format"])
def test_v2_members_and_formats_fail_closed(bundle, tmp_path, change):
    bundle, root = media_case(bundle, tmp_path)
    files = members(export_case_package(bundle, root, planning_history=planning_history(bundle)))
    if change == "missing":
        del files[PLANNING_FILE]
    elif change == "extra_path":
        files["../escape.txt"] = b"unreferenced bytes"
    else:
        manifest = json.loads(files["manifest.json"])
        manifest["format"] = "evidence-trail-case/99"
        files["manifest.json"] = json.dumps(manifest).encode()
    destination = tmp_path / "receiver"
    with pytest.raises(ValueError):
        read_case_package(repack(files), destination)
    assert not destination.exists()
    assert not (tmp_path / "escape.txt").exists()


def test_duplicate_zip_members_fail_before_sidecar_or_artifact_install(bundle, tmp_path):
    bundle, root = media_case(bundle, tmp_path)
    files = members(export_case_package(bundle, root, planning_history=planning_history(bundle)))
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
        with pytest.warns(UserWarning, match="Duplicate name"):
            archive.writestr(PLANNING_FILE, files[PLANNING_FILE])
    destination = tmp_path / "receiver"
    with pytest.raises(ValueError, match="members"):
        read_case_package(output.getvalue(), destination)
    assert not destination.exists()


def test_v2_cannot_be_downgraded_to_silently_discard_its_history(bundle, tmp_path):
    bundle, root = media_case(bundle, tmp_path)
    files = members(export_case_package(bundle, root, planning_history=planning_history(bundle)))
    manifest = json.loads(files["manifest.json"])
    manifest["format"] = FORMAT
    files["manifest.json"] = json.dumps(manifest).encode()
    destination = tmp_path / "receiver"
    with pytest.raises(ValueError, match="exactly the referenced"):
        import_case_package(repack(files), destination)
    assert not destination.exists()


def test_sidecar_path_collision_does_not_break_plain_v1_artifacts(bundle, tmp_path):
    bundle, root = media_case(bundle, tmp_path, reference=PLANNING_FILE)
    destination = tmp_path / "plain"
    assert import_case_package(export_case_package(bundle, root), destination) == bundle
    assert (destination / PLANNING_FILE).is_file()
    with pytest.raises(ValueError, match="conflicts.*sidecar"):
        export_case_package(bundle, root, planning_history=planning_history(bundle))


def test_staged_payload_mutation_fails_before_any_file_write(bundle, tmp_path):
    bundle, root = media_case(bundle, tmp_path)
    destination = tmp_path / "receiver"
    parsed = read_case_package(
        export_case_package(bundle, root), destination, install_artifacts=False
    )
    changed = replace(parsed.artifacts[0], content=b"changed")
    with pytest.raises(ValueError, match="hash mismatch"):
        install_case_artifacts(replace(parsed, artifacts=(changed,)), destination)
    assert not destination.exists()


def test_late_destination_conflict_is_preflighted_before_other_files(bundle, tmp_path):
    bundle, root = media_case(bundle, tmp_path, second=True)
    destination = tmp_path / "receiver"
    parsed = read_case_package(
        export_case_package(bundle, root), destination, install_artifacts=False
    )
    (destination / "media").mkdir(parents=True)
    (destination / "media/second.svg").write_bytes(b"existing different bytes")
    with pytest.raises(ValueError, match="different content"):
        install_case_artifacts(parsed, destination)
    assert not (destination / "media/frame.svg").exists()
    assert (destination / "media/second.svg").read_bytes() == b"existing different bytes"


@pytest.mark.parametrize("race", ["before_skip", "exclusive_open"])
@pytest.mark.parametrize("matching", [False, True])
def test_late_importer_bytes_are_rechecked_and_only_own_files_are_cleaned(
    bundle, tmp_path, monkeypatch, race, matching
):
    bundle, root = media_case(bundle, tmp_path, second=True)
    destination = (tmp_path / "receiver").resolve()
    parsed = read_case_package(
        export_case_package(bundle, root), destination, install_artifacts=False
    )
    first = destination / "media/frame.svg"
    late = destination / "media/second.svg"
    foreign_bytes = (root / "media/second.svg").read_bytes() if matching else b"Conflicting import"
    original_exists, original_open = Path.exists, Path.open
    checks = 0

    def install_foreign_file():
        late.parent.mkdir(parents=True, exist_ok=True)
        with original_open(late, "wb") as target:
            target.write(foreign_bytes)

    def concurrent_exists(path):
        nonlocal checks
        if path == late:
            checks += 1
            if checks == 2:
                install_foreign_file()
        return original_exists(path)

    def concurrent_open(path, *args, **kwargs):
        if path == late and args and args[0] == "xb":
            install_foreign_file()
            raise FileExistsError("Another importer installed the reference")
        return original_open(path, *args, **kwargs)

    if race == "before_skip":
        monkeypatch.setattr(Path, "exists", concurrent_exists)
    else:
        monkeypatch.setattr(Path, "open", concurrent_open)
    if matching:
        assert install_case_artifacts(parsed, destination) == (first,)
        assert first.read_bytes() == (root / "media/frame.svg").read_bytes()
    else:
        with pytest.raises(ValueError, match="different content"):
            install_case_artifacts(parsed, destination)
        assert not first.exists()
    assert late.read_bytes() == foreign_bytes


def test_partial_install_failure_removes_only_new_files(bundle, tmp_path, monkeypatch):
    bundle, root = media_case(bundle, tmp_path, second=True)
    destination = tmp_path / "receiver"
    parsed = read_case_package(
        export_case_package(bundle, root), destination, install_artifacts=False
    )
    original_open = Path.open

    def fail_second(path, *args, **kwargs):
        if path == destination / "media/second.svg" and args and args[0] == "xb":
            raise OSError("Simulated disk failure")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_second)
    with pytest.raises(OSError, match="disk failure"):
        install_case_artifacts(parsed, destination)
    assert not any(path.is_file() for path in destination.rglob("*"))


def test_archive_and_sidecar_size_limits_apply_before_writes(bundle, tmp_path, monkeypatch):
    bundle, root = media_case(bundle, tmp_path)
    history = planning_history(bundle)
    payload = export_case_package(bundle, root, planning_history=history)
    destination = tmp_path / "receiver"
    monkeypatch.setattr(packages, "MAX_PACKAGE_BYTES", len(payload) - 1)
    with pytest.raises(ValueError, match="size limit"):
        read_case_package(payload, destination)
    assert not destination.exists()
    monkeypatch.setattr(packages, "MAX_PACKAGE_BYTES", 64 * 1024 * 1024)
    sidecar_size = len(members(payload)[PLANNING_FILE])
    monkeypatch.setattr(packages, "MAX_FILE_BYTES", sidecar_size - 1)
    with pytest.raises(ValueError, match="Planning history"):
        export_case_package(bundle, root, planning_history=history)
    with pytest.raises(ValueError, match="oversized"):
        read_case_package(payload, destination)
    assert not destination.exists()


def test_packaged_review_history_does_not_rebind_the_plan(tmp_path):
    source = load_demo_bundle()
    history = planning_history(source)
    data = source.model_dump(mode="json")
    data["scenarios"].append(
        {
            "id": "scenario:package",
            "base_run_id": source.findings[0].run_id,
            "excluded_record_ids": [],
            "reason": "Saved assessment",
        }
    )
    data["reviews"].append(
        {
            "id": "review:package",
            "finding_snapshot_id": source.findings[0].snapshot_id,
            "run_id": source.findings[0].run_id,
            "scenario_id": "scenario:package",
            "reviewer": "Evidence reviewer",
            "disposition": "qualify",
            "reason": "Keep the limitation visible.",
            "timestamp": STAMP.isoformat(),
        }
    )
    exported = EvidenceBundle.model_validate(data)
    assert exported.fingerprint() != source.fingerprint()
    assert evidence_fingerprint(data) == evidence_fingerprint(source.model_dump(mode="json"))
    parsed = read_case_package(export_case_package(exported, planning_history=history), tmp_path)
    assert parsed.planning_history == history
    assert (
        parsed.planning_history.revisions[1].binding.source_case_fingerprint == source.fingerprint()
    )
    assert parsed.bundle.reviews == exported.reviews
