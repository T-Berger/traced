"""Verify built artifacts and optionally smoke-test their installed entry points.

Static checks compare package code/resources with the checkout and inspect both
wheel and sdist metadata. The install check rebuilds each sdist outside the
workspace and installs the original wheels with locked dependency constraints
in a disposable environment. Imports and ``--help`` run outside the checkout;
no application factory is invoked and the original artifacts stay unchanged.
"""

from __future__ import annotations

import argparse
import configparser
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import zipfile
from dataclasses import dataclass
from email.parser import BytesParser
from pathlib import Path, PurePosixPath

PROJECTS = {
    "evidence-explorer": "evidence_explorer",
    "swarm-detector": "swarm_detector",
    "operation-silent-storm": "silent_storm",
}
FORBIDDEN_DIRECTORIES = {
    ".git",
    ".venv",
    ".venv-trail",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    "node_modules",
    "dataset",
    "artifacts",
    "backups",
    "exports",
    "outputs",
    "dist",
    "build",
    "htmlcov",
}
SMOKE_MODULES = (
    "evidence_explorer.cli",
    "evidence_explorer.web",
    "evidence_explorer.review.contracts",
    "evidence_explorer.review.integrations.web",
    "swarm_detector.cli",
    "swarm_detector.web",
    "silent_storm.cli",
    "silent_storm.app",
    "silent_storm.shared_brief",
)


class DistributionError(ValueError):
    """A produced artifact violates the distributable package contract."""


@dataclass(frozen=True, slots=True)
class Project:
    directory: Path
    module: str
    name: str
    version: str
    requires_python: str
    scripts: dict[str, str]


def normalized_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def load_projects(root: Path) -> list[Project]:
    projects = []
    for directory, module in PROJECTS.items():
        path = root / directory
        config = tomllib.loads((path / "pyproject.toml").read_text())["project"]
        projects.append(
            Project(
                path,
                module,
                config["name"],
                config["version"],
                config["requires-python"],
                config["scripts"],
            )
        )
    return projects


def forbidden_path(name: str) -> bool:
    """Reject caches, operational data, and environment files; allow example config."""
    parts = PurePosixPath(name).parts
    filename = parts[-1] if parts else ""
    return (
        bool(set(parts) & FORBIDDEN_DIRECTORIES)
        or filename in {".DS_Store", ".coverage", ".env"}
        or (filename.startswith(".env.") and filename not in {".env.example", ".env.sample"})
        or filename.endswith((".pyc", ".pyo", ".db", ".sqlite", ".sqlite3"))
        or any(
            suffix in filename
            for suffix in (
                ".db-wal",
                ".db-shm",
                ".sqlite-wal",
                ".sqlite-shm",
                ".sqlite3-wal",
                ".sqlite3-shm",
            )
        )
        or (
            "data" in parts
            and "src" not in parts
            and not any(package in parts for package in PROJECTS.values())
        )
    )


def validate_member(name: str, seen: set[str]) -> None:
    path = PurePosixPath(name)
    if not name or path.is_absolute() or ".." in path.parts or "\\" in name:
        raise DistributionError(f"Unsafe archive member: {name!r}")
    canonical = str(path)
    if canonical in seen:
        raise DistributionError(f"Duplicate archive member: {name}")
    seen.add(canonical)
    if forbidden_path(name):
        raise DistributionError(f"Operational/private/cache file in distribution: {name}")


def archive_files(path: Path) -> dict[str, bytes]:
    """Inspect archive contents without extracting paths onto the filesystem."""
    files, seen = {}, set()
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as archive:
            for member in archive.infolist():
                validate_member(member.filename, seen)
                if stat.S_ISLNK(member.external_attr >> 16):
                    raise DistributionError(f"Symlink in wheel: {member.filename}")
                if not member.is_dir():
                    files[member.filename] = archive.read(member)
    else:
        with tarfile.open(path, "r:gz") as archive:
            for member in archive.getmembers():
                validate_member(member.name, seen)
                if not member.isfile() and not member.isdir():
                    raise DistributionError(f"Non-regular sdist member: {member.name}")
                if member.isfile():
                    stream = archive.extractfile(member)
                    if stream is None:
                        raise DistributionError(f"Unreadable sdist member: {member.name}")
                    files[member.name] = stream.read()
    return files


def expected_package_files(project: Project) -> dict[str, bytes]:
    source = project.directory / "src"
    files = {}
    for path in sorted((source / project.module).rglob("*")):
        relative = path.relative_to(source).as_posix()
        if path.is_file() and not forbidden_path(relative):
            if path.is_symlink():
                raise DistributionError(f"Package source is a symlink: {path}")
            files[relative] = path.read_bytes()
    if f"{project.module}/__init__.py" not in files:
        raise DistributionError(f"Package source is missing: {project.module}")
    return files


def require_matching_files(
    actual: dict[str, bytes], expected: dict[str, bytes], *, prefix: str = ""
) -> None:
    for name, content in expected.items():
        target = prefix + name
        if target not in actual:
            raise DistributionError(f"Missing package file: {target}")
        if actual[target] != content:
            raise DistributionError(f"Stale or changed package file: {target}")


def inspect_distribution(path: Path, projects: list[Project]) -> dict:
    files = archive_files(path)
    wheel = path.suffix == ".whl"
    candidates = [
        name
        for name in files
        if (
            name.endswith(".dist-info/METADATA")
            if wheel
            else len(PurePosixPath(name).parts) == 2 and name.endswith("/PKG-INFO")
        )
    ]
    if len(candidates) != 1:
        raise DistributionError(f"Expected one package metadata file in {path.name}")
    metadata = BytesParser().parsebytes(files[candidates[0]])
    project = next(
        (
            item
            for item in projects
            if normalized_name(item.name) == normalized_name(metadata.get("Name", ""))
        ),
        None,
    )
    if project is None or metadata.get("Version") != project.version:
        raise DistributionError(f"Unexpected project/version in {path.name}")

    def python_specifiers(value: str) -> list[str]:
        return sorted(part.strip() for part in value.split(","))

    if python_specifiers(metadata.get("Requires-Python", "")) != python_specifiers(
        project.requires_python
    ):
        raise DistributionError(f"Python compatibility metadata differs in {path.name}")
    if any(" @ " in item for item in metadata.get_all("Requires-Dist", [])):
        raise DistributionError(f"Direct URL/path dependency in {path.name}")
    expected = expected_package_files(project)
    if wheel:
        metadata_dir = str(PurePosixPath(candidates[0]).parent)
        entry_points = metadata_dir + "/entry_points.txt"
        if entry_points not in files:
            raise DistributionError(f"Console entry points are missing in {path.name}")
        parser = configparser.ConfigParser()
        parser.optionxform = str
        parser.read_string(files[entry_points].decode())
        scripts = dict(parser["console_scripts"]) if parser.has_section("console_scripts") else {}
        if scripts != project.scripts:
            raise DistributionError(f"Console entry points differ in {path.name}")
        require_matching_files(files, expected)
        unexpected = set(files) - set(expected)
        if any(not name.startswith(metadata_dir + "/") for name in unexpected):
            raise DistributionError(f"Unexpected non-package content in {path.name}")
    else:
        prefix = candidates[0].removesuffix("PKG-INFO")
        if any(not name.startswith(prefix) for name in files):
            raise DistributionError(f"Multiple sdist roots in {path.name}")
        require_matching_files(files, expected, prefix=prefix + "src/")
        actual_source = {
            name.removeprefix(prefix + "src/") for name in files if name.startswith(prefix + "src/")
        }
        if actual_source != set(expected):
            raise DistributionError(f"Unexpected source content in {path.name}")
        required = {
            "pyproject.toml": (project.directory / "pyproject.toml").read_bytes(),
            "README.md": (project.directory / "README.md").read_bytes(),
        }
        require_matching_files(files, required, prefix=prefix)
    return {
        "file": str(path),
        "project": project.name,
        "version": project.version,
        "kind": "wheel" if wheel else "sdist",
        "package_files": len(expected),
        "resources": sum(not name.endswith((".py", ".pyi")) for name in expected),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def check_distributions(root: Path, dist: Path) -> tuple[list[Project], list[dict]]:
    projects = load_projects(root)
    paths = sorted([*dist.glob("*.whl"), *dist.glob("*.tar.gz")])
    if len(paths) != 2 * len(projects):
        raise DistributionError(f"Expected six wheels/sdists in {dist}; found {len(paths)}")
    reports = [inspect_distribution(path, projects) for path in paths]
    expected = {(project.name, kind) for project in projects for kind in ("wheel", "sdist")}
    actual = [(report["project"], report["kind"]) for report in reports]
    if set(actual) != expected or len(set(actual)) != len(actual):
        raise DistributionError("Each project must have exactly one wheel and one sdist")
    return projects, reports


def run(command: list[str], *, cwd: Path, env: dict[str, str], timeout: int = 300) -> str:
    result = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode:
        raise DistributionError(
            f"Command failed ({result.returncode}): {' '.join(command[:3])}\n"
            + result.stderr[-5000:]
            + result.stdout[-2000:]
        )
    return result.stdout


def unpack_sdist(path: Path, destination: Path) -> Path:
    """Copy validated regular members into a fresh directory, without tar extraction."""
    files = archive_files(path)
    roots = {PurePosixPath(name).parts[0] for name in files}
    if len(roots) != 1:
        raise DistributionError(f"Expected one sdist source directory in {path.name}")
    source_root = roots.pop()
    if f"{source_root}/pyproject.toml" not in files:
        raise DistributionError(f"Source build configuration is missing in {path.name}")
    destination.mkdir(parents=True, exist_ok=False)
    for name, content in files.items():
        target = destination.joinpath(*PurePosixPath(name).parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    return destination / source_root


def rebuild_sdists(
    temporary_root: Path,
    projects: list[Project],
    reports: list[dict],
    python: str,
    environment: dict[str, str],
) -> list[dict]:
    """Build portable sdists without workspace sources and compare resulting wheels."""
    results = []
    for report in reports:
        if report["kind"] != "sdist":
            continue
        name = normalized_name(report["project"])
        source = unpack_sdist(Path(report["file"]), temporary_root / "sources" / name)
        output = temporary_root / "rebuilt" / name
        run(
            [
                "uv",
                "build",
                "--no-sources",
                "--wheel",
                "--python",
                python,
                "--no-create-gitignore",
                "--out-dir",
                str(output),
                str(source),
            ],
            cwd=source,
            env=environment,
        )
        wheels = list(output.glob("*.whl"))
        if len(wheels) != 1:
            raise DistributionError(f"Expected one standalone rebuilt wheel for {name}")
        rebuilt = inspect_distribution(wheels[0], projects)
        if rebuilt["project"] != report["project"]:
            raise DistributionError(f"Standalone sdist build changed project identity: {name}")
        original_wheel = next(
            item
            for item in reports
            if item["project"] == report["project"] and item["kind"] == "wheel"
        )
        original_files = archive_files(Path(original_wheel["file"]))
        rebuilt_files = archive_files(wheels[0])
        for filename in ("METADATA", "entry_points.txt"):
            original_metadata = next(
                value
                for member, value in original_files.items()
                if member.endswith(".dist-info/" + filename)
            )
            rebuilt_metadata = next(
                value
                for member, value in rebuilt_files.items()
                if member.endswith(".dist-info/" + filename)
            )
            if original_metadata != rebuilt_metadata:
                raise DistributionError(f"Standalone {name} build changed {filename}")
        results.append(
            {
                "project": report["project"],
                "sdist_sha256": report["sha256"],
                "rebuilt_wheel_sha256": rebuilt["sha256"],
                "package_files": rebuilt["package_files"],
                "metadata_matches_original_wheel": True,
            }
        )
    return results


def installed_smoke(root: Path, projects: list[Project], reports: list[dict], python: str) -> dict:
    """Rebuild sdists and probe installed wheel imports/help in temporary directories."""
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"}
    }
    with tempfile.TemporaryDirectory(prefix="traced-distributions-") as temporary:
        temporary_root = Path(temporary)
        sdist_builds = rebuild_sdists(temporary_root, projects, reports, python, environment)
        venv, runtime = temporary_root / "venv", temporary_root / "runtime"
        runtime.mkdir()
        constraints = temporary_root / "locked-dependencies.txt"
        run(
            [
                "uv",
                "export",
                "--locked",
                "--all-packages",
                "--no-dev",
                "--no-emit-workspace",
                "--no-hashes",
                "--no-header",
                "--output-file",
                str(constraints),
            ],
            cwd=root,
            env=environment,
        )
        run(
            ["uv", "venv", "--no-project", "--python", python, str(venv)],
            cwd=temporary_root,
            env=environment,
        )
        executable_dir = venv / ("Scripts" if os.name == "nt" else "bin")
        interpreter = executable_dir / ("python.exe" if os.name == "nt" else "python")
        wheels = [report["file"] for report in reports if report["kind"] == "wheel"]
        run(
            [
                "uv",
                "pip",
                "install",
                "--python",
                str(interpreter),
                "--constraint",
                str(constraints),
                *wheels,
            ],
            cwd=temporary_root,
            env=environment,
        )
        run(
            ["uv", "pip", "check", "--python", str(interpreter)],
            cwd=temporary_root,
            env=environment,
        )
        environment.update(
            {
                "EVIDENCE_DATA_DIR": str(runtime / "explorer"),
                "EVIDENCE_READ_ONLY": "true",
                "SWARM_DATA_DIR": str(runtime / "swarm"),
                "SWARM_DB_PATH": str(runtime / "swarm/swarm.db"),
                "SILENT_STORM_PROJECT": str(runtime / "silent-storm"),
                "SILENT_STORM_DB": str(runtime / "silent-storm/archive.sqlite3"),
                "SILENT_STORM_DATASET": str(runtime / "missing-dataset"),
                "PORT": "8050",
                "OLLAMA_BASE_URL": "http://127.0.0.1:1",
                "PYTHONDONTWRITEBYTECODE": "1",
            }
        )
        resource_manifest = {
            project.module: {
                name.removeprefix(project.module + "/"): hashlib.sha256(content).hexdigest()
                for name, content in expected_package_files(project).items()
                if not name.endswith((".py", ".pyi"))
            }
            for project in projects
        }
        probe = """
import hashlib, importlib, importlib.resources, json, sys
from pathlib import Path
prefix = Path(sys.prefix).resolve()
for name in json.loads(sys.argv[1]):
    module = importlib.import_module(name)
    assert Path(module.__file__).resolve().is_relative_to(prefix), name
for package, files in json.loads(sys.argv[2]).items():
    root = importlib.resources.files(package)
    for name, expected in files.items():
        assert hashlib.sha256(root.joinpath(name).read_bytes()).hexdigest() == expected, name
print('Installed imports and resource hashes verified.')
"""
        run(
            [
                str(interpreter),
                "-I",
                "-c",
                probe,
                json.dumps(SMOKE_MODULES),
                json.dumps(resource_manifest),
            ],
            cwd=runtime,
            env=environment,
            timeout=60,
        )
        commands = []
        for project in projects:
            for name in project.scripts:
                executable = executable_dir / (name + ".exe" if os.name == "nt" else name)
                output = run(
                    [str(executable), "--help"],
                    cwd=runtime,
                    env=environment,
                    timeout=60,
                )
                if "usage" not in output.lower():
                    raise DistributionError(f"Installed CLI did not show usage: {name}")
                commands.append(name)
        if list(runtime.iterdir()):
            raise DistributionError("Import/--help unexpectedly created workspace data")
        if any(
            hashlib.sha256(Path(item["file"]).read_bytes()).hexdigest() != item["sha256"]
            for item in reports
        ):
            raise DistributionError("An original distribution changed during package verification")
        return {
            "installed_from_wheels": True,
            "locked_dependencies": True,
            "standalone_sdist_builds": sdist_builds,
            "original_artifacts_unchanged": True,
            "imported_modules": list(SMOKE_MODULES),
            "cli_help": commands,
            "workspace_created": False,
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--dist", type=Path, help="Distribution directory; defaults to ROOT/dist")
    parser.add_argument(
        "--install",
        action="store_true",
        help="Rebuild standalone sdists and smoke-test wheel installs",
    )
    parser.add_argument(
        "--python", default=sys.executable, help="Python for the temporary environment"
    )
    parser.add_argument("--report", type=Path, help="Write a JSON verification report")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    try:
        projects, reports = check_distributions(root, (args.dist or root / "dist").resolve())
        result = {"artifacts": reports, "installed_smoke": None}
        if args.install:
            result["installed_smoke"] = installed_smoke(root, projects, reports, args.python)
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(json.dumps(result, indent=2) + "\n")
        print(f"Verified {len(reports)} distributions: code, resources, metadata, file exclusions.")
        if args.install:
            print(
                "Standalone sdist builds and fresh wheel install/import/resource/CLI checks passed."
            )
    except (
        DistributionError,
        OSError,
        subprocess.TimeoutExpired,
        tarfile.TarError,
        zipfile.BadZipFile,
        KeyError,
    ) as error:
        print(f"Distribution check failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
