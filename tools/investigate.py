"""Entry point for ``./traced-investigate`` (show-your-work investigations).

Resolves the same workspace as ``./traced`` (``--data-dir``, EVIDENCE_DATA_DIR,
or the root ``.env``) by reusing its ``configure_environment``, then runs a
command from ``tools/investigation/cli.py``. It is a separate entry point
because ``tools/traced.py`` is under a coordination hold; mounting it there as
``./traced investigate`` is a one-branch follow-up when the hold is released.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.investigation.cli import entry  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(entry(root=ROOT))
