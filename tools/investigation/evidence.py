"""Compatibility import for the packaged investigation evidence module."""

import sys

from evidence_explorer.investigation import evidence as _implementation

# Preserve module identity so existing imports and monkeypatches share one engine.
sys.modules[__name__] = _implementation
