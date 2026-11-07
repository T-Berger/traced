"""Preserve explicitly declared publication precision and document language."""

import json
import re
from typing import Any

import trafilatura

_PUBLICATION_KEYS = {
    "article:published_time",
    "og:published_time",
    "datepublished",
    "pubdate",
    "publishdate",
    "dc.date.issued",
    "dcterms.issued",
    "citation_publication_date",
    "date",
}
_MODIFICATION_KEYS = {
    "article:modified_time",
    "og:updated_time",
    "datemodified",
    "lastmod",
    "last-modified",
    "last_modified",
    "dc.modified",
    "dcterms.modified",
    "modified",
    "updated",
    "revised",
}


def _json_dates(value: Any) -> tuple[list[str], bool]:
    dates, pending, visited = [], [(value, 0)], 0
    has_modified_date = False
    while pending and visited < 1000:
        node, depth = pending.pop()
        visited += 1
        if depth > 8:
            continue
        if isinstance(node, dict):
            if isinstance(node.get("datePublished"), str):
                dates.append(node["datePublished"])
            if isinstance(node.get("dateModified"), str) and node["dateModified"].strip():
                has_modified_date = True
            pending.extend(
                (item, depth + 1) for item in node.values() if isinstance(item, dict | list)
            )
        elif isinstance(node, list):
            pending.extend((item, depth + 1) for item in node if isinstance(item, dict | list))
    return dates, has_modified_date


def declared_metadata(body: bytes) -> dict[str, Any]:
    """Read metadata from already downloaded HTML; never resolve or fetch links."""
    tree = trafilatura.load_html(body)
    if tree is None:
        return {"publication_dates": [], "language": "", "has_modified_date": False}
    language = tree.get("lang") or tree.get("{http://www.w3.org/XML/1998/namespace}lang") or ""
    dates = []
    has_modified_date = False
    for index, element in enumerate(tree.iter("meta")):
        if index >= 300:
            break
        content = element.get("content", "").strip()
        key = (
            element.get("property") or element.get("name") or element.get("itemprop") or ""
        ).lower()
        if key in _PUBLICATION_KEYS and content:
            dates.append(content)
        if content and (
            key in _MODIFICATION_KEYS or element.get("http-equiv", "").lower() == "last-modified"
        ):
            has_modified_date = True
        if not language and key in {"language", "og:locale"}:
            language = content
        if not language and element.get("http-equiv", "").lower() == "content-language":
            language = content
    for index, element in enumerate(tree.iter("script")):
        if index >= 100:
            break
        if element.get("type", "").lower() == "application/ld+json" and element.text:
            try:
                published, modified = _json_dates(json.loads(element.text))
                dates.extend(published)
                has_modified_date |= modified
            except (ValueError, RecursionError):
                continue
    for element in tree.iter("time"):
        if element.get("itemprop", "").lower() == "datemodified" and element.get("datetime"):
            has_modified_date = True
        if (
            element.get("pubdate") is not None
            or element.get("itemprop", "").lower() == "datepublished"
        ):
            if element.get("datetime"):
                dates.append(element.get("datetime"))
    language = language.strip().replace("_", "-")
    if not re.fullmatch(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*", language):
        language = ""
    # Prefer a declared timestamp to a declared day; preserve the original timezone.
    dates.sort(key=lambda value: len(value) <= 10)
    return {
        "publication_dates": dates,
        "language": language,
        "has_modified_date": has_modified_date,
    }
