"""Distribute the selected identity into installable Dash packages.

Run after an approved identity update; --check is read-only and detects drift.
No app needs access to the repository's brand directory at runtime.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BRAND = ROOT / "brand" / "traced"
TARGETS = (
    "evidence-explorer/src/evidence_explorer/assets",
    "evidence-explorer/src/evidence_explorer/review/assets",
    "swarm-detector/src/swarm_detector/assets",
    "operation-silent-storm/src/silent_storm/assets",
)


def expected_assets() -> dict[str, bytes]:
    manifest = json.loads((BRAND / "brand-manifest.json").read_text())
    selected = [
        manifest["primaryLogo"],
        manifest["reversedLogo"],
        manifest["assets"]["symbol"]["svg"],
        manifest["assets"]["favicon"]["svg"],
        manifest["fonts"]["primary"]["file"],
        manifest["fonts"]["primary"]["license"],
        manifest["fonts"]["metadata"]["file"],
        manifest["fonts"]["metadata"]["license"],
    ]
    assets = {f"traced/{name}": (BRAND / name).read_bytes() for name in selected}
    # Dash loads CSS in filename order. Preserve the canonical values while
    # resolving fonts relative to the packaged assets directory.
    tokens = (BRAND / manifest["tokens"]["css"]).read_text()
    assets["00_traced_tokens.css"] = tokens.replace(
        'url("./fonts/', 'url("./traced/fonts/'
    ).encode()
    assets["01_traced_base.css"] = (ROOT / "ui" / "traced-base.css").read_bytes()
    assets["favicon.ico"] = (BRAND / manifest["assets"]["favicon"]["ico"]).read_bytes()
    return assets


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Report missing or changed copies")
    args = parser.parse_args()
    expected = expected_assets()
    changed = []
    for target in TARGETS:
        for name, content in expected.items():
            path = ROOT / target / name
            if path.is_file() and path.read_bytes() == content:
                continue
            changed.append(str(path.relative_to(ROOT)))
            if not args.check:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
    if args.check and changed:
        print("Identity copies differ; run python3 tools/sync_traced_identity.py:")
        print("\n".join(changed))
        return 1
    print(
        f"Traced identity: {len(TARGETS)} app surfaces aligned"
        + (f" ({len(changed)} files updated)." if changed else ".")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
