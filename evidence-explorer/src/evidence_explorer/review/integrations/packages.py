"""Portable evidence cases with validated local artifacts and no network fetches.

The archive contains the complete supplied bundle, including its run/scenario
history, and only artifacts explicitly referenced by its records. Version 2 adds
a hashed planning sidecar without changing native evidence or review identity.
"""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
import zlib
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError

from ..contracts import EvidenceBundle

if TYPE_CHECKING:
    from ...planning_briefs.models import PlanningHistory

FORMAT = "evidence-trail-case/1"
PLANNING_FORMAT = "evidence-trail-case/2"
PLANNING_FILE = "planning-briefs.json"
MAX_PACKAGE_BYTES = 64 * 1024 * 1024
MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_MEMBERS = 5000


class PackageFile(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)

    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    bytes: int = Field(ge=0, strict=True)


class PackageManifest(BaseModel):
    # Preserve optional legacy metadata while interpreting only explicit formats.
    model_config = ConfigDict(extra="allow", frozen=True)
    __pydantic_extra__: dict[str, JsonValue] = Field(init=False)

    format: Literal["evidence-trail-case/1", "evidence-trail-case/2"]
    case_id: str = Field(min_length=1)
    bundle_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    files: dict[str, PackageFile]


@dataclass(frozen=True, slots=True)
class CapturedCasePackage:
    """Evidence and planning read together before their transaction is closed."""

    bundle: EvidenceBundle
    planning_history: PlanningHistory | None = None
    planning_context_token: str | None = None


@dataclass(frozen=True, slots=True)
class PackageArtifact:
    reference: str
    content: bytes
    sha256: str


@dataclass(frozen=True, slots=True)
class ParsedCasePackage:
    bundle: EvidenceBundle
    planning_history: PlanningHistory | None
    manifest: PackageManifest
    artifacts: tuple[PackageArtifact, ...] = ()
    created_artifacts: tuple[Path, ...] = ()


def _validated_planning_history(
    history: PlanningHistory, bundle: EvidenceBundle
) -> PlanningHistory:
    # Keep the plain-v1 codec usable independently of planning initialization.
    from ...planning_briefs.models import PlanningHistory
    from ...planning_briefs.scope import validate_history_for_bundle

    validated = PlanningHistory.model_validate_json(history.model_dump_json())
    validate_history_for_bundle(validated, bundle)
    return validated


def safe_artifact_path(root: Path, reference: str) -> Path:
    """Resolve a supplied relative reference within an explicit trusted root."""
    relative = PurePosixPath(reference)
    if (
        not reference
        or relative.is_absolute()
        or ".." in relative.parts
        or "\\" in reference
        or ":" in reference
        or relative.as_posix() != reference
        or reference in {".", "bundle.json", "manifest.json"}
    ):
        raise ValueError("Evidence artifacts require a safe relative path.")
    root = Path(root).expanduser().resolve()
    path = (root / reference).resolve()
    if not path.is_relative_to(root) or path == root:
        raise ValueError("Evidence artifact escapes its declared root.")
    return path


def _json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _check_existing_artifact(
    path: Path, reference: str, expected_hash: str, expected_bytes: int
) -> None:
    if (
        not path.is_file()
        or path.stat().st_size != expected_bytes
        or _digest(path.read_bytes()) != expected_hash
    ):
        raise ValueError(f"An existing evidence artifact has different content: {reference}")


def _artifact_hashes(bundle: EvidenceBundle) -> dict[str, str]:
    expected: dict[str, str] = {}
    for record in bundle.records:
        if record.artifact_ref:
            if (
                record.artifact_ref in expected
                and expected[record.artifact_ref] != record.content_hash
            ):
                raise ValueError("Two records assign different hashes to the same artifact.")
            expected[record.artifact_ref] = record.content_hash
    return expected


def export_case_package(
    bundle: EvidenceBundle,
    artifact_root: Path | None = None,
    *,
    planning_history: PlanningHistory | None = None,
) -> bytes:
    """Serialize a self-contained selected case, verifying every supplied artifact."""
    bundle = EvidenceBundle.model_validate_json(bundle.model_dump_json())
    files = {"bundle.json": _json(bundle.model_dump(mode="json"))}
    if len(files["bundle.json"]) > MAX_FILE_BYTES:
        raise ValueError("Case bundle exceeds the small-case package limit.")
    package_format = FORMAT
    if planning_history is not None:
        history = _validated_planning_history(planning_history, bundle)
        if not history.revisions:
            raise ValueError("A planning sidecar requires relevant revision history.")
        if PLANNING_FILE in _artifact_hashes(bundle):
            raise ValueError("An evidence artifact conflicts with the planning sidecar path.")
        files[PLANNING_FILE] = _json(history.model_dump(mode="json"))
        if len(files[PLANNING_FILE]) > MAX_FILE_BYTES:
            raise ValueError("Planning history exceeds the small-case package limit.")
        package_format = PLANNING_FORMAT
    for reference, expected in _artifact_hashes(bundle).items():
        if artifact_root is None:
            raise ValueError("This case needs an artifact root to export its supplied media.")
        path = safe_artifact_path(artifact_root, reference)
        if not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
            raise ValueError(f"Evidence artifact missing or exceeds the file limit: {reference}")
        content = path.read_bytes()
        if _digest(content) != expected:
            raise ValueError(f"Saved artifact hash mismatch: {reference}")
        files[reference] = content
    if len(files) + 1 > MAX_MEMBERS or sum(map(len, files.values())) > MAX_PACKAGE_BYTES:
        raise ValueError("Case exceeds the small-case package limit.")
    manifest = {
        "format": package_format,
        "case_id": bundle.case.id,
        "bundle_fingerprint": bundle.fingerprint(),
        "files": {
            name: {"sha256": _digest(data), "bytes": len(data)} for name, data in files.items()
        },
    }
    files["manifest.json"] = _json(manifest)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(files.items()):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, data)
    result = output.getvalue()
    if len(result) > MAX_PACKAGE_BYTES:
        raise ValueError("Case archive exceeds the package size limit.")
    return result


def import_case_package(data: bytes, artifact_root: Path) -> EvidenceBundle:
    """Read plain v1; callers retaining planning history must use the typed reader."""
    return _read_case_package(
        data, artifact_root, allow_planning=False, install_artifacts=True
    ).bundle


def read_case_package(
    data: bytes, artifact_root: Path, *, install_artifacts: bool = True
) -> ParsedCasePackage:
    """Validate native evidence and optional planning before writing any artifact."""
    return _read_case_package(
        data, artifact_root, allow_planning=True, install_artifacts=install_artifacts
    )


def _read_case_package(
    data: bytes, artifact_root: Path, *, allow_planning: bool, install_artifacts: bool
) -> ParsedCasePackage:
    if len(data) > MAX_PACKAGE_BYTES:
        raise ValueError("Case archive exceeds the package size limit.")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            members = archive.infolist()
            names = [item.filename for item in members]
            if (
                len(members) > MAX_MEMBERS
                or len(names) != len(set(names))
                or sum(item.file_size for item in members) > MAX_PACKAGE_BYTES
                or any(item.file_size > MAX_FILE_BYTES for item in members)
                or any(item.is_dir() or item.flag_bits & 1 for item in members)
            ):
                raise ValueError("Invalid or oversized evidence package members.")
            if not {"manifest.json", "bundle.json"} <= set(names):
                raise ValueError("Case package requires its manifest and bundle.")
            try:
                manifest = PackageManifest.model_validate_json(archive.read("manifest.json"))
            except ValidationError as exc:
                raise ValueError(
                    "Unsupported evidence package format or malformed manifest."
                ) from exc
            if manifest.format == PLANNING_FORMAT and not allow_planning:
                raise ValueError(
                    "This v2 package includes planning history. Use read_case_package or the "
                    "workspace importer to retain it."
                )
            if set(names) != {"manifest.json", *manifest.files}:
                raise ValueError("Package members do not match its manifest.")
            content = {name: archive.read(name) for name in manifest.files}
            for name, payload in content.items():
                declared = manifest.files[name]
                if _digest(payload) != declared.sha256 or len(payload) != declared.bytes:
                    raise ValueError(f"Package file verification failed: {name}")
            bundle = EvidenceBundle.model_validate_json(content["bundle.json"])
            if (
                bundle.case.id != manifest.case_id
                or bundle.fingerprint() != manifest.bundle_fingerprint
            ):
                raise ValueError("Package case identity does not match its bundle.")
            expected = _artifact_hashes(bundle)
            planning_history = None
            sidecars: set[str] = set()
            if manifest.format == PLANNING_FORMAT:
                from ...planning_briefs.models import PlanningHistory

                if PLANNING_FILE not in content or PLANNING_FILE in expected:
                    raise ValueError("Planning package requires its reserved planning sidecar.")
                planning_history = _validated_planning_history(
                    PlanningHistory.model_validate_json(content[PLANNING_FILE]), bundle
                )
                if not planning_history.revisions:
                    raise ValueError("A planning sidecar requires relevant revision history.")
                sidecars.add(PLANNING_FILE)
            if set(content) != {"bundle.json", *expected, *sidecars}:
                raise ValueError("Package must contain exactly the referenced evidence artifacts.")
            artifacts = []
            for reference, digest in expected.items():
                path = safe_artifact_path(artifact_root, reference)
                if _digest(content[reference]) != digest:
                    raise ValueError(f"Saved artifact hash mismatch: {reference}")
                if path.exists():
                    _check_existing_artifact(path, reference, digest, len(content[reference]))
                artifacts.append(PackageArtifact(reference, content[reference], digest))
    except zlib.error as exc:
        raise ValueError(
            "Invalid evidence package: the compressed data is damaged. Choose a valid case package."
        ) from exc
    except (
        zipfile.BadZipFile,
        KeyError,
        TypeError,
        json.JSONDecodeError,
        UnicodeError,
    ) as exc:
        raise ValueError(f"Invalid evidence package: {exc}") from exc

    parsed = ParsedCasePackage(bundle, planning_history, manifest, tuple(artifacts))
    if install_artifacts:
        parsed = replace(parsed, created_artifacts=install_case_artifacts(parsed, artifact_root))
    return parsed


def install_case_artifacts(package: ParsedCasePackage, artifact_root: Path) -> tuple[Path, ...]:
    """Install verified immutable bytes after database conflict checks have passed.

    All preflight checks happen before the first write. Only newly created files
    are returned, so a transaction owner can remove them if its commit fails.
    """
    bundle = EvidenceBundle.model_validate_json(package.bundle.model_dump_json())
    expected = _artifact_hashes(bundle)
    by_reference = {artifact.reference: artifact for artifact in package.artifacts}
    if len(by_reference) != len(package.artifacts) or set(by_reference) != set(expected):
        raise ValueError("Staged artifacts do not match the referenced evidence artifacts.")
    if sum(len(artifact.content) for artifact in package.artifacts) > MAX_PACKAGE_BYTES:
        raise ValueError("Staged artifacts exceed the package size limit.")
    for reference, expected_hash in expected.items():
        artifact = by_reference[reference]
        if (
            len(artifact.content) > MAX_FILE_BYTES
            or artifact.sha256 != expected_hash
            or _digest(artifact.content) != expected_hash
        ):
            raise ValueError(f"Saved artifact hash mismatch: {reference}")
        path = safe_artifact_path(artifact_root, reference)
        if path.exists():
            _check_existing_artifact(path, reference, expected_hash, len(artifact.content))
    created: list[Path] = []
    try:
        for reference in expected:
            # Another importer may have installed this logical reference since
            # preflight. Resolve it again and never silently accept other bytes.
            path = safe_artifact_path(artifact_root, reference)
            artifact = by_reference[reference]
            if path.exists():
                _check_existing_artifact(path, reference, artifact.sha256, len(artifact.content))
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                target = path.open("xb")
            except FileExistsError:
                path = safe_artifact_path(artifact_root, reference)
                _check_existing_artifact(path, reference, artifact.sha256, len(artifact.content))
                continue
            with target:
                created.append(path)
                target.write(artifact.content)
    except Exception:
        for path in created:
            path.unlink(missing_ok=True)
        raise
    return tuple(created)
