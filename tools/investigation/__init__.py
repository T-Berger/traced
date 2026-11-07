"""Show-your-work investigations over frozen Maltego Data MCP records.

Two separate roles:

* the **investigator** runs a few explicit Maltego lookups (each frozen as a
  hashed record by ``evidence_explorer.collection.maltego``) and has a model
  write claims, each with reasoning steps, exact quotes from saved record text,
  and explicit assumptions. Its output is an ordinary ``EvidenceBundle``.
* the **checker** follows every link of such a bundle back to the saved record:
  it re-verifies quotes, offsets, and hashes mechanically, then runs targeted,
  mostly deterministic reasoning checks and labels every link ``supported``,
  ``unsupported``, ``assumption_not_backed``, ``contradicted``, or ``unchecked``,
  showing which conclusions depend on a flagged link.

A label is a machine check of traceability and consistency against the saved
records. It does not establish truth, ownership, control, or intent, and it is
not a human review decision.

This package lives under ``tools/`` while the shared ``evidence_explorer``
tree and ``tools/traced.py`` are under a coordination freeze; it imports those
modules and edits none of them.
"""

__all__ = ["CHECKER_VERSION", "INVESTIGATOR_VERSION"]

CHECKER_VERSION = "1"
INVESTIGATOR_VERSION = "1"
