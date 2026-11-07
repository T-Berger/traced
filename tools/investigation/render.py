"""Compatibility import for the packaged investigation render module."""

import sys

from evidence_explorer.investigation import render as _implementation

# Preserve module identity so existing imports and monkeypatches share one engine.
sys.modules[__name__] = _implementation
