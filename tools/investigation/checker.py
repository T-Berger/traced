"""Compatibility import for the packaged investigation checker module."""

import sys

from evidence_explorer.investigation import checker as _implementation

# Preserve module identity so existing imports and monkeypatches share one engine.
sys.modules[__name__] = _implementation
