"""Project a bounded trace graph from frozen local artifacts; stdlib, no I/O network.

This does not import Traced, ingest a case, run a model, or change source files.
Graph IDs for posts/captures are display identities, not new native evidence IDs.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import unittest
import zipfile
from collections import Counter, defaultdict
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlsplit

CASE_DIR = "collector-11e5afc822bd4ce15ac5"
ENRICH = "maltego-enrichment"
MCP = "https://datamcp.maltego.com/mcp"
DIWALI = "1980606111838355647"
FOLLOWUP = "1980606660528804219"
MUSK = "1985692613010502114"


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def post_id(url):
    parsed = urlsplit(url or "")
    if parsed.hostname not in {"x.com", "www.x.com", "twitter.com", "www.twitter.com"}:
        return None
    match = re.fullmatch(r"/[^/]+/status/(\d+)/?", parsed.path)
    return match.group(1) if match else None


def utc(value):
    parsed = datetime.fromisoformat(value)
    if parsed.utcoffset() is None:
        raise ValueError("Naive timestamp")
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")


def exact_quote(record, fragment):
    selector = fragment["selector"]
    if selector["kind"] != "text":
        raise ValueError("Only exact saved text selectors are supported")
    start, end = selector["start"], selector["end"]
    if not (0 <= start < end <= len(record["text"])):
        raise ValueError("Invalid saved fragment offsets")
    if record["text"][start:end] != fragment["quote"]:
        raise ValueError("Saved fragment does not match exact record text")
    return {
        "record_id": record["id"],
        "fragment_id": fragment["id"],
        "start": start,
        "end": end,
        "quote": fragment["quote"],
        "text_sha256": record["content_hash"],
    }


class Projection:
    def __init__(self, root):
        self.root = root.resolve()
        self.inputs = {}
        self.nodes = {}
        self.edges = []
        self.clusters = []
        self.post_captures = defaultdict(list)
        self.post_dates = defaultdict(list)

    def read(self, relative, expected=None, *, raw=False):
        path = (self.root / relative).resolve()
        if not path.is_relative_to(self.root) or not path.is_file():
            raise ValueError("Input must be a regular file beneath the artifact root")
        data = path.read_bytes()
        if len(data) > 30_000_000:
            raise ValueError("Artifact exceeds bounded input size")
        hashed = digest(data)
        if expected is not None and hashed != expected:
            raise ValueError(f"Input digest mismatch: {relative}")
        self.inputs[relative] = {"path": relative, "sha256": hashed, "bytes": len(data)}
        return data if raw else json.loads(data)

    def ref(self, relative, **fields):
        return {"artifact": relative, "artifact_sha256": self.inputs[relative]["sha256"], **fields}

    def receipt(self, relative):
        receipt = self.read(relative)
        if receipt["endpoint"] != MCP or receipt["result"].get("isError"):
            raise ValueError("Unexpected MCP endpoint or failed result")
        if digest(canonical(receipt["result"])) != receipt["result_sha256"]:
            raise ValueError("MCP result digest mismatch")
        content = receipt["result"]["content"]
        if len(content) != 1 or content[0]["type"] != "text":
            raise ValueError("Expected one JSON MCP result")
        return receipt, json.loads(content[0]["text"])

    def node(self, ident, kind, label, **fields):
        if ident in self.nodes:
            raise ValueError(f"Duplicate graph node: {ident}")
        self.nodes[ident] = {
            "id": ident,
            "kind": kind,
            "label": label,
            "evidence_refs": [],
            **fields,
        }
        return ident

    def edge(self, source, target, kind, label, basis, refs, **fields):
        if source not in self.nodes or target not in self.nodes:
            raise ValueError("Dangling graph edge")
        value = {
            "source": source,
            "target": target,
            "kind": kind,
            "label": label,
            "basis": basis,
            "evidence_refs": refs,
            "review_status": "unreviewed",
            **fields,
        }
        value["id"] = "edge:" + digest(canonical(value))[:24]
        if any(e["id"] == value["id"] for e in self.edges):
            raise ValueError("Duplicate graph edge")
        self.edges.append(value)
        return value["id"]

    def post(self, native_id, source_url, refs):
        if not re.fullmatch(r"\d+", native_id) or post_id(source_url) != native_id:
            raise ValueError("Post ID and public source URL disagree")
        ident = "post:x:" + native_id
        if ident not in self.nodes:
            self.node(
                ident,
                "post",
                "X post " + native_id,
                platform="x",
                native_id=native_id,
                source_url=source_url,
                record_ids=[],
                published_at=None,
                publication_assertions=[],
                evidence_refs=[],
            )
        self.nodes[ident]["evidence_refs"].extend(refs)
        return ident

    def captured(self, node_id, item, refs, *, complete=True):
        ident = self.post(item["native_id"], item["source_url"], refs)
        if self.nodes[node_id].get("record_id"):
            self.nodes[ident]["record_ids"].append(self.nodes[node_id]["record_id"])
        self.edge(
            node_id,
            ident,
            "capture_of_same_post" if complete else "excerpt_of_post",
            "Capture of same X post" if complete else "Saved excerpt of X post",
            "Exact platform post ID from the saved source identity/locator. This binds identity, not identical text or independent corroboration.",
            refs,
            identity_basis="x post ID",
            text_sha256=self.nodes[node_id].get("text_sha256"),
        )
        self.post_captures[ident].append(node_id)
        if item.get("published_at"):
            self.post_dates[ident].append(
                {"published_at": utc(item["published_at"]), "capture_node_id": node_id}
            )
        return ident

    def account(self, post, author_id, handle, label, refs):
        # Unknown numeric IDs remain unknown; no account-control or person-identity claim.
        ident = "account:x:" + (author_id or "handle:" + handle.casefold())
        if ident not in self.nodes:
            self.node(
                ident,
                "account",
                label or "@" + handle,
                platform="x",
                author_id=author_id,
                handle=handle,
                source_url="https://x.com/" + handle,
                evidence_refs=[],
            )
        self.nodes[ident]["evidence_refs"].extend(refs)
        self.edge(
            post,
            ident,
            "authored_by",
            "Source-attributed account",
            "Saved source identifies this public account as author. Account control, real-person identity, and intent are not independently verified.",
            refs,
        )

    def group(self, ident, label, kind, members, edge_ids, basis, limits):
        self.clusters.append(
            {
                "id": ident,
                "label": label,
                "kind": kind,
                "member_ids": sorted(set(members)),
                "edge_ids": sorted(set(edge_ids)),
                "basis": basis,
                "limits": limits,
            }
        )


def build(root):
    p = Projection(root)
    zip_path = f"{CASE_DIR}/mamdani-election-demo.zip"
    p.read(zip_path, raw=True)
    with zipfile.ZipFile(root / zip_path) as archive:
        if sum(i.file_size for i in archive.infolist()) > 5_000_000:
            raise ValueError("Case package exceeds bounded expanded size")
        bundle_raw = archive.read("bundle.json")
        manifest = json.loads(archive.read("manifest.json"))
        if digest(bundle_raw) != manifest["files"]["bundle.json"]["sha256"]:
            raise ValueError("Native package bundle hash mismatch")
        bundle = json.loads(bundle_raw)
    dash_path = f"{CASE_DIR}/dashboard-data.json"
    dashboard = p.read(dash_path)
    binding = dashboard["binding"]
    if (
        binding["package_sha256"] != p.inputs[zip_path]["sha256"]
        or binding["bundle_sha256"] != digest(bundle_raw)
        or binding["case_id"] != bundle["case"]["id"]
        or binding["case_fingerprint"] != manifest["bundle_fingerprint"]
    ):
        raise ValueError("Dashboard and native package bindings disagree")
    records = {r["id"]: r for r in bundle["records"]}
    fragments = {f["id"]: f for f in bundle["fragments"]}
    findings = {f["snapshot_id"]: f for f in bundle["findings"]}
    for r in records.values():
        if digest(r["text"].encode()) != r["content_hash"]:
            raise ValueError("Native record text hash mismatch")
        refs = [
            p.ref(zip_path, member="bundle.json", record_id=r["id"], text_sha256=r["content_hash"])
        ]
        metadata = r["metadata"]
        p.node(
            r["id"],
            "capture" if metadata.get("platform") == "x" else "source_record",
            r["source_label"],
            record_id=r["id"],
            source_url=r["source_url"],
            published_at=r["published_at"],
            acquired_at=r["acquired_at"],
            text_preview=r["text"][:280],
            text_length=len(r["text"]),
            text_sha256=r["content_hash"],
            capture_method="pinned_collector_dump"
            if metadata.get("collector")
            else "saved_public_excerpt",
            evidence_refs=refs,
        )
        native_id = metadata.get("native_id") or post_id(r["source_url"])
        if native_id:
            item = {
                "native_id": native_id,
                "source_url": r["source_url"],
                "published_at": r["published_at"],
            }
            post = p.captured(r["id"], item, refs, complete=metadata.get("platform") == "x")
            if metadata.get("platform") == "x":
                handle = metadata["collector_post"]["handle"]
                p.account(post, metadata.get("author_id"), handle, metadata["channel_name"], refs)
    for fragment in fragments.values():
        exact_quote(records[fragment["record_id"]], fragment)
    for finding in findings.values():
        role = finding["details"].get("role", "premise")
        kind = "draft_claim" if role == "claim" else "draft_" + role
        p.node(
            finding["snapshot_id"],
            kind,
            finding["details"].get("label", role),
            finding_id=finding["snapshot_id"],
            run_id=finding["run_id"],
            statement=finding["statement"],
            human_review="pending",
            model_generated=False,
            evidence_refs=[
                p.ref(zip_path, member="bundle.json", finding_id=finding["snapshot_id"])
            ],
        )
    for link in bundle["links"]:
        refs = [p.ref(zip_path, member="bundle.json", link_id=link["id"])]
        quotes = [
            exact_quote(records[fragments[f]["record_id"]], fragments[f])
            for f in link["fragment_ids"]
        ]
        p.edge(
            link["source"]["id"],
            link["target"]["id"],
            "declared_draft_evidence" if link["source"]["type"] == "record" else "draft_dependency",
            "Draft-declared " + link["relation"],
            link["basis"],
            refs,
            native_link_id=link["id"],
            native_relation=link["relation"],
            run_id=link["run_id"],
            exact_source_quotes=quotes,
            meaning="Supplied draft relationship; not a fact verdict or human approval.",
        )
    for link in dashboard["relationships"]["declared_parent_links"]:
        for side in ("source", "target"):
            if (
                records[link[side + "_record_id"]]["metadata"]["native_id"]
                != link[side + "_native_id"]
            ):
                raise ValueError("Declared parent native identity mismatch")
        p.edge(
            "post:x:" + link["source_native_id"],
            "post:x:" + link["target_native_id"],
            "declared_parent",
            "Collector-declared parent",
            link["meaning"],
            [p.ref(dash_path, json_pointer="/relationships/declared_parent_links", **link)],
            collector_flags={k: v for k, v in link.items() if k.startswith("collector_is_")},
        )

    # New captures remain outside the native package and carry no native record IDs.
    vm_path = f"{ENRICH}/vetric-source-import.manifest.json"
    vm = p.read(vm_path)
    vi_path = f"{ENRICH}/vetric-source-import.json"
    vi = p.read(vi_path, vm["source_import"]["sha256"])
    describe_path = f"{ENRICH}/live-describe-vetric/1-maltego_describe.json"
    _, described = p.receipt(describe_path)
    described = {item["id"]: item for item in described["items"]}
    for item, entry in zip(vi["items"], vm["items"], strict=True):
        path = f"{ENRICH}/{entry['receipt_path']}"
        p.read(path, entry["receipt_sha256"], raw=True)
        receipt, result = p.receipt(path)
        entity = next(e for e in result["entities"] if e["ref"] == entry["entity_ref"])
        if (
            entity["type"] != "maltego.Twit"
            or entity["id"] != item["native_id"]
            or entity["content"] != item["text"]
            or digest(canonical(entity)) != entry["entity_sha256"]
            or entry["text_sha256"] != digest(item["text"].encode())
            or receipt["arguments"] != entry["request_original_input"]
            or utc(parsedate_to_datetime(entity["pubdate"]).isoformat()) != item["published_at"]
            or utc(receipt["completed_at"]) != item["acquired_at"]
        ):
            raise ValueError("Vetric projection does not match saved receipt")
        refs = [
            p.ref(path, result_sha256=receipt["result_sha256"], entity_ref=entity["ref"]),
            p.ref(vi_path, source_item_index=entry["source_item_index"]),
        ]
        node = "capture:vetric:" + digest(entity["ref"].encode())[:24]
        p.node(
            node,
            "capture",
            "Vetric · @" + entry["author_handle"],
            record_id=None,
            native_id=item["native_id"],
            source_url=item["source_url"],
            published_at=item["published_at"],
            acquired_at=item["acquired_at"],
            text_preview=item["text"][:280],
            text_length=len(item["text"]),
            text_sha256=entry["text_sha256"],
            snapshot_counts=entry["snapshot_counts"],
            capture_method="vetric_via_maltego_datamcp",
            native_case_imported=False,
            evidence_refs=refs,
        )
        post = p.captured(node, item, refs)
        p.account(post, item["author_id"], entry["author_handle"], item["channel_name"], refs)
        requested = entry["requested_post_id"]
        if requested != item["native_id"]:
            capability = described[receipt["arguments"]["capability_id"]]
            if capability["id"] != "ci_3Ea4-YAxI1G0xD1TQpR6g93YCi":
                raise ValueError("Unexpected parent-producing transform")
            child = "post:x:" + requested
            if child not in p.nodes:
                raise ValueError("Parent lookup input has no saved selected child post")
            p.edge(
                child,
                post,
                "provider_returned_parent",
                "Vetric returned original post",
                capability["description"]
                + " The requested input and returned ID are distinct; no quote/reply subtype is inferred from this response alone.",
                refs + [p.ref(describe_path, capability_id=capability["id"])],
                requested_native_id=requested,
                returned_native_id=item["native_id"],
                capability_id=capability["id"],
            )

    public_path = f"{ENRICH}/public-cross-check/source_import.receipt.json"
    public_receipt = p.read(public_path)
    import_path = f"{ENRICH}/public-cross-check/source_import.json"
    public_import = p.read(import_path, public_receipt["source_import_sha256"])
    for item, entry in zip(public_import["items"], public_receipt["records"], strict=True):
        response_path = f"{ENRICH}/public-cross-check/{entry['response_file']}"
        p.read(response_path, entry["response_sha256"], raw=True)
        if (
            item["native_id"] != entry["native_id"]
            or digest(item["text"].encode()) != entry["text_sha256"]
        ):
            raise ValueError("Public fallback projection mismatch")
        refs = [
            p.ref(import_path, native_id=item["native_id"], text_sha256=entry["text_sha256"]),
            p.ref(response_path, response_record_id=entry["response_record_id"]),
        ]
        node = "capture:oembed:" + item["native_id"]
        p.node(
            node,
            "capture",
            "Official X oEmbed · " + item["channel_name"],
            record_id=None,
            native_id=item["native_id"],
            source_url=item["source_url"],
            published_at=None,
            publication_date_only=entry["date_only"],
            publication_timezone=None,
            acquired_at=item["acquired_at"],
            text_preview=item["text"][:280],
            text_length=len(item["text"]),
            text_sha256=entry["text_sha256"],
            capture_method="official_x_oembed_public_fallback",
            native_case_imported=False,
            evidence_refs=refs,
        )
        post = p.captured(node, item, refs)
        handle = urlsplit(item["source_url"]).path.split("/")[1]
        # Attach only when numeric author ID is independently supplied for this same exact post;
        # otherwise retain a handle-only identity with an explicitly unknown public ID.
        identity_entry = next((e for e in vm["items"] if e["native_id"] == item["native_id"]), None)
        numeric = identity_entry["author_public_id"] if identity_entry else None
        account_refs = list(refs)
        if identity_entry:
            account_refs.append(
                p.ref(
                    f"{ENRICH}/{identity_entry['receipt_path']}",
                    entity_ref=identity_entry["author_public_id_entity_ref"],
                    author_id_basis="Separate saved public author entity for this exact post",
                )
            )
        if numeric is None:
            matching = [
                r for r in records.values() if r["metadata"].get("native_id") == item["native_id"]
            ]
            numeric = matching[0]["metadata"].get("author_id") if matching else None
            if matching:
                account_refs.append(
                    p.ref(
                        zip_path,
                        member="bundle.json",
                        record_id=matching[0]["id"],
                        author_id_basis="Separate saved collector identity for this exact post",
                    )
                )
        p.account(post, numeric, handle, item["channel_name"], account_refs)

    archive_path = f"{ENRICH}/live-archive/1-maltego_run.json"
    receipt, archive_result = p.receipt(archive_path)
    replays = {}
    for path in sorted((root / ENRICH / "archive-cross-check").glob("*-receipt.json")):
        relative = str(path.relative_to(root))
        replay = p.read(relative)
        raw_path = str(path.parent.relative_to(root) / replay["raw_file"])
        raw = p.read(raw_path, replay["sha256"], raw=True)
        if len(raw) != replay["byte_length"] or replay["source_url"] in replays:
            raise ValueError("Archive replay size or uniqueness mismatch")
        replays[replay["source_url"]] = (replay, relative, raw_path)
    for entity in archive_result["entities"]:
        if entity["type"] != "maltego.wayback.Snapshot":
            raise ValueError("Unexpected archive result type")
        native_id = post_id(entity["originalUrl"])
        if entity["originalUrl"] != receipt["arguments"]["entity_value"]:
            raise ValueError("Archive original URL differs from exact request")
        when = datetime.strptime(entity["rawTimestamp"], "%Y%m%d%H%M%S").replace(tzinfo=UTC)
        if int(when.timestamp() * 1000) != int(entity["dateTime"]):
            raise ValueError("Archive timestamp fields disagree")
        refs = [
            p.ref(archive_path, result_sha256=receipt["result_sha256"], entity_ref=entity["ref"])
        ]
        replay_info = replays.get(entity["archiveUrl"])
        replay_fields = {
            "body_retrieved": False,
            "post_content_recovered": None,
            "replay_status": "not_checked",
            "replay_acquired_at": None,
        }
        replay_meaning = "This replay was not checked; only provider locator metadata is available."
        if replay_info:
            replay, replay_path, replay_raw_path = replay_info
            if (
                replay["original_source_url"] != entity["originalUrl"]
                or utc(replay["archive_capture_time"]) != when.isoformat().replace("+00:00", "Z")
                or replay["assessment"]["availability"] != "http_success_post_content_unavailable"
            ):
                raise ValueError("Archive replay binding or supported assessment mismatch")
            replay_fields = {
                "body_retrieved": True,
                "post_content_recovered": False,
                "replay_status": "checked_error_shell",
                "replay_acquired_at": utc(replay["completed_at"]),
                "replay_http_status": replay["http_status"],
                "replay_assessment": replay["assessment"],
                "replay_sha256": replay["sha256"],
            }
            refs.extend([p.ref(replay_path), p.ref(replay_raw_path)])
            replay_meaning = "Saved direct replay HTTP 200 contains an X error/application shell; original post content was not recovered. No linked scripts were executed."
        node = "archive:" + entity["rawTimestamp"] + ":" + native_id
        p.node(
            node,
            "archive_pointer",
            "Wayback · " + when.isoformat().replace("+00:00", "Z"),
            source_url=entity["archiveUrl"],
            original_url=entity["originalUrl"],
            archive_timestamp=when.isoformat().replace("+00:00", "Z"),
            acquired_at=utc(receipt["completed_at"]),
            provider_reported_http_status=entity["statusCode"],
            evidence_refs=refs,
            **replay_fields,
        )
        p.edge(
            node,
            "post:x:" + native_id,
            "archive_points_to",
            "Archive locator for post",
            "Provider metadata names this exact original URL. "
            + replay_meaning
            + " Returned provider locators are capped at five; archive timestamps are not publication times.",
            refs,
        )
    returned_archive_urls = {e["archiveUrl"] for e in archive_result["entities"]}
    if set(replays) - returned_archive_urls:
        raise ValueError("A saved replay has no exact locator in the bounded archive result")

    web_path = f"{ENRICH}/live-web-search/1-maltego_run.json"
    receipt, web = p.receipt(web_path)
    search = "lookup:" + web["run_id"]
    p.node(
        search,
        "saved_search",
        "Saved ballot/fusion-voting search",
        query=receipt["arguments"]["entity_value"],
        acquired_at=utc(receipt["completed_at"]),
        evidence_refs=[p.ref(web_path, result_sha256=receipt["result_sha256"])],
    )
    candidates = defaultdict(list)
    for entity in web["entities"]:
        if entity["type"] == "maltego.URL":
            candidates[entity["url"]].append((entity, entity.get("short-title"), None))
        for snippet in entity.get("search_engine_results", []):
            title, url, text = snippet.split("|:~", 2)
            candidates[url].append((entity, title, text))
    web_members = [search]
    web_edges = []
    for url, results in sorted(candidates.items()):
        refs = [
            p.ref(web_path, entity_ref=e["ref"], result_sha256=receipt["result_sha256"])
            for e, _, _ in results
        ]
        node = "web-result:" + digest(url.encode())[:24]
        snippets = [html.unescape(re.sub(r"<[^>]+>", "", s)) for _, _, s in results if s]
        out_of_scope = "/2026/10/" in url
        p.node(
            node,
            "web_result",
            html.unescape(results[0][1] or url),
            source_url=url,
            text_preview=(snippets[0][:280] if snippets else None),
            full_text_retrieved=False,
            provider_entities_collapsed=len(results),
            published_at=None,
            scope_status="outside_requested_dates_by_saved_locator_and_snippet"
            if out_of_scope
            else "publication_date_not_independently_verified",
            evidence_refs=refs,
        )
        web_edges.append(
            p.edge(
                search,
                node,
                "discovered_web_result",
                "Search returned candidate URL",
                "Same exact URL is one candidate document even when returned as Website and URL entities. Search snippets are not full-text evidence or independent corroboration.",
                refs,
            )
        )
        web_members.append(node)
    p.group(
        "group:web-search",
        "Separate web-search leads",
        "saved_search_results",
        web_members,
        web_edges,
        "Exact URLs deduplicated within the saved provider result.",
        [
            "One October 2026 result is outside the requested dates.",
            "No claim-support edges are inferred from search wording.",
        ],
    )

    # Official CSV stays separate from the native PDF excerpt: a shared report title is
    # insufficient to invent a byte-level identity or a new support edge.
    boe_path = f"{ENRICH}/public-cross-check/nyc-mayor-candidate-lines.json"
    boe = p.read(boe_path)
    csv_path = f"{ENRICH}/public-cross-check/nyc-mayor-recap.response"
    p.read(csv_path, boe["response_sha256"], raw=True)
    csv_receipt_path = f"{ENRICH}/public-cross-check/nyc-mayor-recap.receipt.json"
    csv_receipt = p.read(csv_receipt_path)
    if csv_receipt["response_sha256"] != boe["response_sha256"]:
        raise ValueError("Official CSV receipt hash mismatch")
    p.node(
        boe["response_record_id"],
        "source_record",
        "NYC BOE · saved citywide Mayor CSV",
        record_id=None,
        source_url=boe["source_url"],
        published_at=None,
        acquired_at=csv_receipt["retrieved_at"],
        native_case_imported=False,
        evidence_refs=[
            p.ref(csv_path),
            p.ref(csv_receipt_path),
            p.ref(boe_path, row_numbers=[r["row_number"] for r in boe["citywide_candidate_rows"]]),
        ],
        row_number_basis=boe["row_number_basis"],
        declared_candidate_lines=boe["citywide_candidate_rows"],
        meaning="Saved official result rows; no individual ballot audit or human finding is implied.",
    )

    for ident, captures in sorted(p.post_captures.items()):
        node = p.nodes[ident]
        node["publication_assertions"] = p.post_dates[ident]
        times = sorted({x["published_at"] for x in p.post_dates[ident]})
        node["published_at"] = times[0] if len(times) == 1 else None
        node["publication_status"] = (
            "source_times_agree"
            if len(times) == 1
            else "conflicting_source_times"
            if times
            else "exact_time_unknown"
        )
        node["record_ids"] = sorted(set(node["record_ids"]))
        node["capture_node_ids"] = captures
        node["distinct_saved_text_hashes"] = len({p.nodes[c]["text_sha256"] for c in captures})
        node["text_variant_meaning"] = (
            "Different capture text hashes may reflect excerpts or link-display formatting; they are not automatically edits, contradictions, or tampering."
        )
        if len(captures) > 1:
            members = [ident, *captures]
            edges = [e["id"] for e in p.edges if e["source"] in captures and e["target"] == ident]
            p.group(
                "group:captures:" + node["native_id"],
                "Captures · X " + node["native_id"],
                "same_post_captures",
                members,
                edges,
                "Exact X native post ID, with each capture retained separately.",
                [
                    "Same post identity does not mean identical captured text or independent corroboration."
                ],
            )
    paired = ["post:x:" + FOLLOWUP, "post:x:" + DIWALI]
    parent_edges = [
        e for e in p.edges if e["kind"] in {"declared_parent", "provider_returned_parent"}
    ]
    p.group(
        "group:diwali-parent",
        "Diwali · declared and returned parent",
        "parent_relationship_comparison",
        paired,
        [e["id"] for e in parent_edges],
        "Collector parent ID and separate Vetric original-post response resolve the same exact selected pair.",
        [
            "Parent/quote relationships alone do not establish coordination, intent, or account control."
        ],
    )
    incoming = defaultdict(list)
    for edge in p.edges:
        if edge["kind"] in {"declared_draft_evidence", "draft_dependency"}:
            incoming[edge["target"]].append(edge)
    for ident, finding in findings.items():
        if finding["details"].get("role") != "claim":
            continue
        members, edge_ids, stack = {ident}, set(), [ident]
        while stack:
            target = stack.pop()
            for edge in incoming[target]:
                edge_ids.add(edge["id"])
                if edge["source"] not in members:
                    members.add(edge["source"])
                    stack.append(edge["source"])
        p.group(
            "group:draft:" + finding["details"]["label"],
            "Draft chain · " + finding["details"]["label"],
            "native_draft_evidence_chain",
            members,
            edge_ids,
            "Only native saved links, exact quoted fragments, run IDs, and finding snapshots.",
            [
                "No new provider capture is silently attached to an existing claim.",
                "Draft support is not a factual finding or a human decision.",
            ],
        )
    # Archive pointers form a bounded trace family independently of account/search edges.
    archive_edges = [e for e in p.edges if e["kind"] == "archive_points_to"]
    p.group(
        "group:ballot-archives",
        "Musk ballot post · archive pointers",
        "archive_locator_family",
        ["post:x:" + MUSK, *[e["source"] for e in archive_edges]],
        [e["id"] for e in archive_edges],
        "Exact original post URL in five provider-returned snapshot locators.",
        [
            "Two saved replays returned X error/application shells without original post content; the other three remain untested.",
            "Five capture locators are not five independent sources.",
        ],
    )
    graph = {
        "schema": "traced.evidence-trace-graph/1",
        "binding": binding,
        "inputs": sorted(p.inputs.values(), key=lambda x: x["path"]),
        "scope": {
            "timezone": dashboard["scope"]["timezone"],
            "event_at": dashboard["scope"]["event_at"],
            "windows": dashboard["scope"]["windows"],
            "native_case_records": len(records),
            "selected_collector_posts": dashboard["scope"]["selected_collector_posts"],
            "saved_corpus_rows": dashboard["scope"]["saved_corpus_rows"],
            "coverage_gaps": dashboard["scope"]["coverage_gaps"],
            "projection_basis": "All 36 native case records plus six new public/provider post captures, five archive pointers, three deduplicated web leads, and saved BOE CSV metadata; no full corpus expansion.",
        },
        "nodes": list(p.nodes.values()),
        "edges": p.edges,
        "clusters": p.clusters,
        "similarity": {
            "status": "not_computed",
            "candidate_clusters": [],
            "reason": "Exact post identities, recorded parent relationships, and native draft evidence chains are sufficient. No token overlap, common topic, or shared account is promoted to coordination.",
        },
        "limits": [
            "Graph is a local companion projection; native case, IDs, findings, planning history, and reviews are unchanged.",
            "Post and capture display node IDs are not imported evidence identities. record_id/finding_id identify only existing native objects.",
            "New captures and external candidates have no native support edges or human review.",
            "Different text hashes across capture formats do not prove tampering or an edited post.",
            "Account edges are source assertions, not identity verification, automation, coordination, ownership, or intent findings.",
            "Disconnected official CSV and web leads are intentional: no evidentiary link is invented from topical similarity.",
            "The saved Seth post is not linked to Musk by an inferred spread/quotation edge; the supplied captures alone do not establish that relationship.",
            "Preview text is bounded; exact fragment offsets and original hashed artifacts remain authoritative.",
            "No source app execution, model, live request, database write, or approval occurred.",
        ],
        "actions_performed": {
            "live_calls": 0,
            "model_calls": 0,
            "canonical_imports": 0,
            "database_writes": 0,
            "new_reviews": 0,
        },
    }
    validate_graph(graph)
    graph["counts"] = {
        "nodes": len(p.nodes),
        "edges": len(p.edges),
        "clusters": len(p.clusters),
        "node_kinds": dict(sorted(Counter(n["kind"] for n in p.nodes.values()).items())),
        "edge_kinds": dict(sorted(Counter(e["kind"] for e in p.edges).items())),
    }
    return graph


def validate_graph(graph):
    nodes = {n["id"] for n in graph["nodes"]}
    edges = {e["id"]: e for e in graph["edges"]}
    if len(nodes) != len(graph["nodes"]) or len(edges) != len(graph["edges"]):
        raise ValueError("Duplicate graph identity")
    for edge in edges.values():
        if edge["source"] not in nodes or edge["target"] not in nodes or not edge["evidence_refs"]:
            raise ValueError("Unbound edge")
    if "inputs" in graph:
        inputs = {item["path"]: item["sha256"] for item in graph["inputs"]}
        for value in [*graph["nodes"], *graph["edges"]]:
            for ref in value["evidence_refs"]:
                if inputs.get(ref["artifact"]) != ref["artifact_sha256"]:
                    raise ValueError("Graph reference does not resolve to the pinned input")
    for group in graph["clusters"]:
        members = set(group["member_ids"])
        if not members <= nodes or not set(group["edge_ids"]) <= edges.keys():
            raise ValueError("Unresolved group member")
        for edge_id in group["edge_ids"]:
            if {edges[edge_id]["source"], edges[edge_id]["target"]} - members:
                raise ValueError("Group edge escapes declared members")


class ProjectionTests(unittest.TestCase):
    def test_exact_public_post_identity(self):
        self.assertEqual(post_id("https://twitter.com/user/status/123?x=1"), "123")
        self.assertIsNone(post_id("https://example.com/user/status/123"))
        self.assertIsNone(post_id("https://x.com/user/status/no-number"))

    def test_quote_rejects_changed_text(self):
        record = {"id": "r", "text": "saved", "content_hash": digest(b"saved")}
        fragment = {"id": "f", "quote": "save", "selector": {"kind": "text", "start": 0, "end": 4}}
        self.assertEqual(exact_quote(record, fragment)["quote"], "save")
        fragment["quote"] = "fake"
        with self.assertRaises(ValueError):
            exact_quote(record, fragment)

    def test_dangling_edge_rejected(self):
        graph = {
            "nodes": [{"id": "a"}],
            "edges": [{"id": "e", "source": "a", "target": "b", "evidence_refs": [{}]}],
            "clusters": [],
        }
        with self.assertRaises(ValueError):
            validate_graph(graph)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root", type=Path, help="Directory containing the saved case and receipts"
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(ProjectionTests)
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        raise SystemExit(0 if result.wasSuccessful() else 1)
    if args.root is None:
        parser.error("--root is required when building the trace graph")
    graph = build(args.root.resolve())
    output = args.output or args.root / CASE_DIR / "trace-graph.json"
    if not output.resolve().is_relative_to(args.root.resolve()):
        raise ValueError("Output must remain inside the artifact directory")
    relative_output = str(output.resolve().relative_to(args.root.resolve()))
    if relative_output in {item["path"] for item in graph["inputs"]}:
        raise ValueError("Output may not replace a source artifact")
    data = (json.dumps(graph, ensure_ascii=False, indent=2) + "\n").encode()
    output.write_bytes(data)
    print(
        json.dumps(
            {
                "output": str(output.resolve()),
                "sha256": digest(data),
                "bytes": len(data),
                "counts": graph["counts"],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
