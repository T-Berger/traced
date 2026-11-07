"""Compatibility namespace for investigation command-line imports.

The reusable engine lives in ``evidence_explorer.investigation``; the CLI stays
here to reuse the repository launcher and its workspace selection.
"""

from evidence_explorer.investigation import CHECKER_VERSION, INVESTIGATOR_VERSION

__all__ = ["CHECKER_VERSION", "INVESTIGATOR_VERSION"]
