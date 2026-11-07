"""Regression coverage for distributable artifact failures, without building apps."""

from __future__ import annotations

import io
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path

from tools.check_distributions import (
    PROJECTS,
    DistributionError,
    archive_files,
    check_distributions,
    unpack_sdist,
    validate_member,
)


def write_wheel(path: Path, members: dict[str, bytes]) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in members.items():
            archive.writestr(name, content)


def write_sdist(path: Path, members: dict[str, bytes]) -> None:
    with tarfile.open(path, "w:gz") as archive:
        for name, content in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))


class DistributionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="distribution-tests-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.dist = self.root / "dist"
        self.dist.mkdir()
        for directory, module in PROJECTS.items():
            project = self.root / directory
            source = project / "src" / module
            source.mkdir(parents=True)
            (source / "assets").mkdir()
            (source / "data").mkdir()
            files = {
                f"{module}/__init__.py": b'"""Package fixture."""\n',
                f"{module}/assets/theme.css": b"body { color: navy; }\n",
                f"{module}/data/example.json": b'{"synthetic": true}\n',
            }
            for name, content in files.items():
                (project / "src" / name).write_bytes(content)
            name = module.replace("_", "-")
            pyproject = (
                f'[project]\nname = "{name}"\nversion = "0.1.0"\n'
                'requires-python = ">=3.12,<3.13"\n[project.scripts]\n'
                f'{name} = "{module}.cli:main"\n'
            ).encode()
            (project / "pyproject.toml").write_bytes(pyproject)
            (project / "README.md").write_bytes(b"Fixture documentation.\n")
            metadata = (
                f"Metadata-Version: 2.4\nName: {name}\nVersion: 0.1.0\n"
                "Requires-Python: <3.13,>=3.12\n"
            ).encode()
            dist_info = f"{module}-0.1.0.dist-info"
            write_wheel(
                self.dist / f"{module}-0.1.0-py3-none-any.whl",
                {
                    **files,
                    f"{dist_info}/METADATA": metadata,
                    f"{dist_info}/entry_points.txt": (
                        f"[console_scripts]\n{name} = {module}.cli:main\n"
                    ).encode(),
                },
            )
            prefix = f"{module}-0.1.0/"
            write_sdist(
                self.dist / f"{module}-0.1.0.tar.gz",
                {
                    **{prefix + "src/" + name: content for name, content in files.items()},
                    prefix + "PKG-INFO": metadata,
                    prefix + "pyproject.toml": pyproject,
                    prefix + "README.md": b"Fixture documentation.\n",
                },
            )

    def test_all_three_artifacts_preserve_assets_data_scripts_and_python_metadata(self):
        projects, reports = check_distributions(self.root, self.dist)
        self.assertEqual(len(projects), 3)
        self.assertEqual(len(reports), 6)
        self.assertEqual({item["resources"] for item in reports}, {2})

    def test_changed_source_resource_rejects_stale_build(self):
        (self.root / "evidence-explorer/src/evidence_explorer/assets/theme.css").write_bytes(b"new")
        with self.assertRaisesRegex(DistributionError, "Stale or changed"):
            check_distributions(self.root, self.dist)

    def test_missing_demo_data_rejects_wheel(self):
        path = self.dist / "evidence_explorer-0.1.0-py3-none-any.whl"
        members = archive_files(path)
        del members["evidence_explorer/data/example.json"]
        write_wheel(path, members)
        with self.assertRaisesRegex(DistributionError, "Missing package file"):
            check_distributions(self.root, self.dist)

    def test_wrong_console_script_rejects_wheel(self):
        path = self.dist / "silent_storm-0.1.0-py3-none-any.whl"
        members = archive_files(path)
        members["silent_storm-0.1.0.dist-info/entry_points.txt"] = b"[console_scripts]\n"
        write_wheel(path, members)
        with self.assertRaisesRegex(DistributionError, "entry points differ"):
            check_distributions(self.root, self.dist)

    def test_extra_old_distribution_is_rejected(self):
        (self.dist / "old-0.0.1.tar.gz").write_bytes(b"old")
        with self.assertRaisesRegex(DistributionError, "Expected six"):
            check_distributions(self.root, self.dist)

    def test_operational_files_are_rejected_but_examples_are_allowed(self):
        for name in (
            "package/.env",
            "package/.env.local",
            "package/dataset/archive.zip",
            "package/data/archive.json",
            "package/archive.sqlite3-wal",
            "package/src/module/__pycache__/file.pyc",
            "package/.venv/bin/python",
        ):
            with self.subTest(name=name), self.assertRaisesRegex(DistributionError, "Operational"):
                validate_member(name, set())
        validate_member("package/.env.example", set())
        validate_member("package/src/module/data/example.json", set())

    def test_unsafe_and_duplicate_members_are_rejected(self):
        for name in ("../outside", "/absolute", "folder\\file"):
            with self.subTest(name=name), self.assertRaisesRegex(DistributionError, "Unsafe"):
                validate_member(name, set())
        seen = set()
        validate_member("package/file", seen)
        with self.assertRaisesRegex(DistributionError, "Duplicate"):
            validate_member("package/./file", seen)

    def test_sdist_symlink_is_rejected_without_extraction(self):
        path = self.dist / "malformed.tar.gz"
        with tarfile.open(path, "w:gz") as archive:
            info = tarfile.TarInfo("package/link")
            info.type = tarfile.SYMTYPE
            info.linkname = "/outside"
            archive.addfile(info)
        with self.assertRaisesRegex(DistributionError, "Non-regular"):
            archive_files(path)

    def test_sdist_unpack_is_self_contained_and_leaves_original_artifacts_unchanged(self):
        originals = {path.name: path.read_bytes() for path in self.dist.iterdir()}
        destination = self.root / "standalone-source"
        source = unpack_sdist(self.dist / "silent_storm-0.1.0.tar.gz", destination)
        self.assertEqual(source.parent, destination)
        self.assertTrue((source / "pyproject.toml").is_file())
        self.assertEqual(
            (source / "src/silent_storm/data/example.json").read_bytes(), b'{"synthetic": true}\n'
        )
        self.assertEqual(originals, {path.name: path.read_bytes() for path in self.dist.iterdir()})
        with self.assertRaises(FileExistsError):
            unpack_sdist(self.dist / "silent_storm-0.1.0.tar.gz", destination)


if __name__ == "__main__":
    unittest.main()
