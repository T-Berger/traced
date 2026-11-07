"""A shared case must remain inspectable on another reviewer's computer."""

import hashlib
import io
import json
import zipfile

import pytest

from evidence_explorer.review.contracts import EvidenceBundle
from evidence_explorer.review.demo import load_demo_bundle
from evidence_explorer.review.integrations.packages import (
    export_case_package,
    import_case_package,
    safe_artifact_path,
)


def test_text_case_round_trip_preserves_all_content(tmp_path):
    bundle = load_demo_bundle()
    restored = import_case_package(export_case_package(bundle), tmp_path / "new-computer")
    assert restored.model_dump() == bundle.model_dump()


def _media_case(tmp_path):
    bundle = load_demo_bundle().model_dump(mode="json")
    content = b'<svg xmlns="http://www.w3.org/2000/svg"><text>Synthetic fixture</text></svg>'
    root = tmp_path / "original"
    (root / "media").mkdir(parents=True)
    (root / "media/frame.svg").write_bytes(content)
    bundle["records"].append(
        {
            "id": "package:image",
            "kind": "image",
            "source_label": "Supplied synthetic frame",
            "artifact_ref": "media/frame.svg",
            "content_hash": hashlib.sha256(content).hexdigest(),
        }
    )
    return EvidenceBundle.model_validate(bundle), root, content


def test_media_package_is_portable_and_repeatable(tmp_path):
    bundle, root, content = _media_case(tmp_path)
    package = export_case_package(bundle, root)
    assert package == export_case_package(bundle, root)
    destination = tmp_path / "another-computer"
    restored = import_case_package(package, destination)
    assert (destination / "media/frame.svg").read_bytes() == content
    assert restored.fingerprint() == bundle.fingerprint()
    assert import_case_package(package, destination).fingerprint() == restored.fingerprint()


def test_media_mutation_fails_before_export(tmp_path):
    bundle, root, _ = _media_case(tmp_path)
    (root / "media/frame.svg").write_text("changed")
    with pytest.raises(ValueError, match="hash mismatch"):
        export_case_package(bundle, root)


def test_import_will_not_replace_existing_evidence(tmp_path):
    bundle, root, _ = _media_case(tmp_path)
    destination = tmp_path / "existing"
    (destination / "media").mkdir(parents=True)
    (destination / "media/frame.svg").write_bytes(b"different")
    with pytest.raises(ValueError, match="different content"):
        import_case_package(export_case_package(bundle, root), destination)
    assert (destination / "media/frame.svg").read_bytes() == b"different"


@pytest.mark.parametrize(
    "reference",
    [
        "../secret",
        "/etc/passwd",
        "a/../../secret",
        "C:/x",
        "a\\x",
        "frames//same.svg",
        "./frames/same.svg",
        "frames/./same.svg",
    ],
)
def test_local_artifact_paths_stay_in_explicit_root(tmp_path, reference):
    with pytest.raises(ValueError):
        safe_artifact_path(tmp_path, reference)


def test_symlink_to_external_file_is_rejected(tmp_path):
    root = tmp_path / "artifacts"
    root.mkdir()
    (root / "escape").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError, match="escapes"):
        safe_artifact_path(root, "escape/source.txt")


def test_corrupted_archive_does_not_write_artifacts(tmp_path):
    bundle, root, _ = _media_case(tmp_path)
    package = export_case_package(bundle, root)
    damaged = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(package)) as source, zipfile.ZipFile(damaged, "w") as target:
        for name in source.namelist():
            payload = source.read(name)
            if name == "bundle.json":
                parsed = json.loads(payload)
                parsed["case"]["title"] = "Tampered title"
                payload = json.dumps(parsed).encode()
            target.writestr(name, payload)
    destination = tmp_path / "new"
    with pytest.raises(ValueError, match="verification failed"):
        import_case_package(damaged.getvalue(), destination)
    assert not destination.exists()


@pytest.mark.parametrize(
    "manifest",
    [
        None,
        [],
        "invalid",
        {
            "format": "evidence-trail-case/1",
            "files": {"bundle.json": None},
        },
    ],
)
def test_malformed_manifest_is_rejected_before_writing_artifacts(tmp_path, manifest):
    package = io.BytesIO()
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        archive.writestr("bundle.json", "{}")
    destination = tmp_path / "new"
    with pytest.raises(ValueError, match="format|verification failed"):
        import_case_package(package.getvalue(), destination)
    assert not destination.exists()
