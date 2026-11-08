"""Load the policy-guidelines folder: platforms, cases, regulations and documentation guidance."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# traced/evidence-explorer/src/evidence_explorer/policies/catalog.py -> traced/
_PROJECT_ROOT = Path(__file__).resolve().parents[4]
_REQUIRED = {
    "platform": ("id", "name", "policies", "reporting"),
    "case": ("id", "subject", "categories"),
    "regulation": ("id", "title", "summary"),
}


def policy_dir() -> Path:
    return Path(os.getenv("TRACED_POLICY_DIR") or _PROJECT_ROOT / "policy-guidelines")


@dataclass
class Catalog:
    root: Path
    platforms: dict[str, dict[str, Any]] = field(default_factory=dict)
    cases: dict[str, dict[str, Any]] = field(default_factory=dict)
    regulations: dict[str, dict[str, Any]] = field(default_factory=dict)
    documentation: dict[str, Any] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)


def _read(path: Path, kind: str, problems: list[str]) -> dict | None:
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError) as exc:
        problems.append(f"{path.name}: could not be read ({exc.__class__.__name__}).")
        return None
    missing = [k for k in _REQUIRED.get(kind, ()) if k not in data]
    if missing:
        problems.append(f"{path.relative_to(path.parents[1])}: missing {', '.join(missing)}.")
        return None
    data["_path"] = path
    return data


def load_catalog(root: Path | None = None) -> Catalog:
    root = Path(root or policy_dir())
    cat = Catalog(root=root)
    if not root.is_dir():
        cat.problems.append(f"Policy folder not found: {root}")
        return cat
    for path in sorted(root.glob("*/platform.json")):
        if data := _read(path, "platform", cat.problems):
            cat.platforms[data["id"]] = data
    for path in sorted((root / "cases").glob("*.json")):
        if data := _read(path, "case", cat.problems):
            cat.cases[data["id"]] = data
    for path in sorted((root / "regulations").glob("*.json")):
        if data := _read(path, "regulation", cat.problems):
            cat.regulations[data["id"]] = data
    doc = root / "common" / "how-to-document.json"
    if doc.is_file():
        cat.documentation = _read(doc, "documentation", cat.problems) or {}
    return cat
