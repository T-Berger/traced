"""Finding-level origin labels without inheriting case or source-record provenance."""

from collections.abc import Mapping


def claim_synthetic_origin(finding: Mapping) -> bool | None:
    """Read an explicit marker, or its exact pinned source finding in legacy results.

    An existing top-level key is authoritative even when null or invalid. Only
    a claim-check result with an absent key may use the pinned declaration.
    Case/record flags and the legacy ``source_synthetic`` summary never supply it.
    """
    details = finding.get("details")
    if not isinstance(details, Mapping):
        return None
    synthetic = details.get("synthetic")
    if details.get("domain") == "claim_check" and "synthetic" not in details:
        lineage = details.get("claim_check")
        source = lineage.get("source_finding_snapshot") if isinstance(lineage, Mapping) else None
        source_details = source.get("details") if isinstance(source, Mapping) else None
        synthetic = source_details.get("synthetic") if isinstance(source_details, Mapping) else None
    return synthetic if synthetic is True or synthetic is False else None


def claim_origin_label(finding: Mapping) -> str | None:
    """Show a claim-origin label only for a claim-check finding."""
    details = finding.get("details")
    if not isinstance(details, Mapping) or details.get("domain") != "claim_check":
        return None
    synthetic = claim_synthetic_origin(finding)
    if synthetic is True:
        return "Synthetic test claim"
    if synthetic is False:
        return "Declared non-synthetic claim; factual accuracy requires review"
    return "Claim origin not specified"


def claim_origin_sentence(origin: str) -> str:
    """Keep exported unknown-origin wording explicit without a repeated prefix."""
    return f"{origin}." if origin == "Claim origin not specified" else f"Claim origin: {origin}."
