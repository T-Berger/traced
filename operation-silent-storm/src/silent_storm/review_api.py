"""JSON endpoints for persistent reviews and scenario-scoped source exclusions."""

from __future__ import annotations

from urllib.parse import urlsplit

from flask import Flask, abort, jsonify, request

from .contracts import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    ReviewConflictError,
    ReviewNotFoundError,
    ReviewServiceProtocol,
    normalize_filter,
)


def _body(allowed: set[str]) -> dict:
    if not request.is_json:
        abort(415, description="Send review changes as application/json.")
    origin = request.headers.get("Origin")
    if origin:
        parsed = urlsplit(origin)
        if parsed.scheme not in {"http", "https"} or parsed.netloc != request.host:
            abort(403, description="Review changes must come from this workspace.")
    value = request.get_json()
    if not isinstance(value, dict):
        raise ValueError("A JSON object is required.")
    unknown = set(value) - allowed
    if unknown:
        raise ValueError(f"Unknown review fields: {', '.join(sorted(unknown))}.")
    return value


def _version(value, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a nonnegative integer.")
    return value


def _page_args() -> dict[str, int]:
    result = {}
    for name, default, maximum in (
        ("page", 1, 1_000_000),
        ("page_size", DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE),
    ):
        try:
            value = int(request.args.get(name, default))
        except (ValueError, TypeError) as exc:
            raise ValueError(f"{name} must be an integer.") from exc
        if not 1 <= value <= maximum:
            raise ValueError(f"{name} must be between 1 and {maximum}.")
        result[name] = value
    return result


def register_review_routes(server: Flask, reviews: ReviewServiceProtocol) -> None:
    @server.errorhandler(ReviewConflictError)
    def stale_review(error):
        return jsonify(error=str(error), conflict=True), 409

    @server.errorhandler(ReviewNotFoundError)
    def missing_review(error):
        return jsonify(error=str(error)), 404

    @server.route("/api/releases/<release_id>/scenarios", methods=["GET", "POST"])
    def scenarios(release_id: str):
        if request.method == "GET":
            return jsonify(reviews.list_scenarios(release_id))
        body = _body({"name", "reviewer", "source_scenario_id"})
        return jsonify(
            reviews.create_scenario(
                release_id,
                body.get("name", ""),
                body.get("reviewer", ""),
                source_scenario_id=body.get("source_scenario_id"),
            )
        ), 201

    @server.get("/api/scenarios/<scenario_id>")
    def scenario_detail(scenario_id: str):
        return jsonify(reviews.get_scenario(scenario_id))

    @server.route("/api/scenarios/<scenario_id>/reviews", methods=["GET", "POST"])
    def scenario_reviews(scenario_id: str):
        if request.method == "GET":
            return jsonify(
                reviews.list_reviews(
                    scenario_id,
                    status=normalize_filter(request.args.get("status")),
                    query=request.args.get("query", ""),
                    week=normalize_filter(request.args.get("week")),
                    **_page_args(),
                )
            )
        fields = {
            "title",
            "observation",
            "interpretation",
            "uncertainty",
            "rationale",
            "reviewer",
            "evidence_ids",
            "status",
            "review_id",
            "expected_revision",
        }
        body = _body(fields)
        return jsonify(
            reviews.save_review(
                scenario_id,
                title=body.get("title", ""),
                observation=body.get("observation", ""),
                interpretation=body.get("interpretation", ""),
                uncertainty=body.get("uncertainty", ""),
                rationale=body.get("rationale", ""),
                reviewer=body.get("reviewer", ""),
                evidence_ids=body.get("evidence_ids", []),
                status=body.get("status", "draft"),
                review_id=body.get("review_id"),
                expected_revision=_version(body.get("expected_revision", 0), "expected_revision"),
            )
        ), 200 if body.get("review_id") else 201

    @server.get("/api/reviews/<review_id>")
    def review_detail(review_id: str):
        return jsonify(reviews.get_review(review_id))

    @server.get("/api/reviews/<review_id>/history")
    def review_history(review_id: str):
        return jsonify(reviews.review_history(review_id, **_page_args()))

    @server.get("/api/scenarios/<scenario_id>/exclusions/preview")
    def exclusion_preview(scenario_id: str):
        supplied = request.args.get("excluded", "true").lower()
        if supplied not in {"true", "false", "1", "0"}:
            raise ValueError("excluded must be true or false.")
        return jsonify(
            reviews.exclusion_preview(
                scenario_id,
                request.args.get("video_id", ""),
                excluded=supplied in {"true", "1"},
            )
        )

    @server.route("/api/scenarios/<scenario_id>/exclusions", methods=["GET", "POST"])
    def exclusions(scenario_id: str):
        if request.method == "GET":
            return jsonify(reviews.exclusion_history(scenario_id, **_page_args()))
        body = _body({"video_id", "excluded", "reason", "reviewer", "expected_version"})
        if not isinstance(body.get("excluded"), bool):
            raise ValueError("excluded must be a JSON boolean.")
        return jsonify(
            reviews.set_source_exclusion(
                scenario_id,
                body.get("video_id", ""),
                excluded=body["excluded"],
                reason=body.get("reason", ""),
                reviewer=body.get("reviewer", ""),
                expected_version=_version(body.get("expected_version"), "expected_version"),
            )
        )

    @server.get("/api/scenarios/<scenario_id>/chronology")
    def chronology(scenario_id: str):
        return jsonify(
            reviews.chronology(
                scenario_id,
                unit=request.args.get("unit", "videos"),
                status=normalize_filter(request.args.get("status")),
            )
        )
