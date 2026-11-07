"""Compose the specialist Dash app, bounded evidence routes, and review APIs.

Archive inspection routes are read-only; registered review and delivery handlers
also support explicit writes to specialist state. App creation initializes any
default stores/services but never imports or seeds an archive. The shared-package
exporter reuses Explorer's contract without constructing its app or worker.
"""

from __future__ import annotations

from pathlib import Path

from dash import Dash
from flask import abort, jsonify, request, send_file
from werkzeug.exceptions import HTTPException

from .config import AppConfig
from .contracts import (
    CONTACT_SHEET_SIZE,
    MAX_PAGE_SIZE,
    ExplorerFilters,
    ExplorerStore,
    ReviewServiceProtocol,
    normalize_filter,
)


def create_app(
    store: ExplorerStore | None = None,
    config: AppConfig | None = None,
    review_service: ReviewServiceProtocol | None = None,
    delivery_service=None,
    diff_service=None,
) -> Dash:
    """Wire supplied services or initialize defaults in the specialist database.

    Missing services are built from the selected store, including review and
    brief schemas. The returned app includes both read routes and explicit
    review/delivery actions; it does not import or seed a dataset.
    """
    from .briefs import BriefService
    from .delivery_api import register_delivery_routes
    from .media import resolve_frame_path
    from .release_diff import ReleaseDiffService
    from .review_api import register_review_routes
    from .reviews import ReviewService
    from .shared_brief import SharedBriefExporter
    from .storage import Store
    from .ui.delivery import register_delivery_callbacks
    from .ui.explorer import create_layout, register_callbacks
    from .ui.review import register_review_callbacks

    settings = config or AppConfig.from_env()
    repository = store if store is not None else Store(settings.db_path)
    review_repository = review_service if review_service is not None else ReviewService(repository)
    brief_repository = (
        delivery_service
        if delivery_service is not None
        else BriefService(
            repository,
            review_repository,
        )
    )
    release_comparison = (
        diff_service if diff_service is not None else ReleaseDiffService(repository)
    )
    brief_exporter = SharedBriefExporter(repository, brief_repository)
    application = Dash(
        __name__,
        title="Traced · Historical Evidence Workbench",
        assets_folder=str(Path(__file__).with_name("assets")),
        suppress_callback_exceptions=True,
        update_title="Loading evidence…",
    )
    server = application.server
    server.config["EXPLORER_STORE"] = repository
    server.config["REVIEW_SERVICE"] = review_repository
    server.config["BRIEF_SERVICE"] = brief_repository
    server.config["RELEASE_DIFF_SERVICE"] = release_comparison
    # A 2 MiB JSON upload expands to about 2.67 MiB in Dash's base64 envelope.
    server.config["MAX_CONTENT_LENGTH"] = 3 * 1024 * 1024
    server.config["SILENT_STORM_CONFIG"] = settings
    # Layout is rebuilt for each page load so a later CLI import is discoverable.
    application.layout = lambda: create_layout(
        repository,
        review_repository,
        brief_repository,
        release_comparison,
    )

    def require_release(release_id: str) -> dict:
        try:
            release = repository.get_release(release_id)
        except KeyError:
            release = None
        if release is None:
            abort(404, description="Release not found.")
        return release

    def integer_argument(name: str, default: int, minimum: int, maximum: int) -> int:
        value = request.args.get(name)
        if value is None:
            return default
        try:
            result = int(value)
        except (ValueError, TypeError) as exc:
            raise ValueError(f"{name} must be an integer.") from exc
        if not minimum <= result <= maximum:
            raise ValueError(f"{name} must be between {minimum} and {maximum}.")
        return result

    @server.errorhandler(HTTPException)
    def http_error(error: HTTPException):
        return jsonify(error=error.description), error.code

    @server.errorhandler(ValueError)
    def invalid_request(error: ValueError):
        return jsonify(error=str(error)), 400

    @server.after_request
    def content_headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @server.get("/health")
    def health():
        releases = repository.list_releases()
        return {
            "status": "ok" if releases else "awaiting_import",
            "ready": bool(releases),
            "releases": len(releases),
        }, 200 if releases else 503

    @server.get("/api/releases")
    def releases():
        return jsonify(repository.list_releases())

    @server.get("/api/releases/<release_id>/overview")
    def overview(release_id: str):
        require_release(release_id)
        return jsonify(repository.overview(release_id))

    @server.get("/api/releases/<release_id>/clips")
    def clips(release_id: str):
        require_release(release_id)
        filters = ExplorerFilters(
            query=request.args.get("query", ""),
            week=normalize_filter(request.args.get("week")),
            side=normalize_filter(request.args.get("side")),
            camera=normalize_filter(request.args.get("camera")),
            image_type=normalize_filter(request.args.get("image_type")),
            page=integer_argument("page", 1, 1, 1_000_000),
            page_size=integer_argument("page_size", 12, 1, MAX_PAGE_SIZE),
        )
        return jsonify(repository.list_clips(release_id, filters))

    @server.get("/api/releases/<release_id>/clips/<clip_key>")
    def clip(release_id: str, clip_key: str):
        require_release(release_id)
        result = repository.get_clip(
            release_id,
            clip_key,
            offset=integer_argument("offset", 0, 0, 100_000_000),
            limit=integer_argument("limit", CONTACT_SHEET_SIZE, 1, MAX_PAGE_SIZE),
        )
        if result is None:
            abort(404, description="Clip not found.")
        return jsonify(result)

    @server.get("/api/releases/<release_id>/frames/<frame_id>")
    def frame(release_id: str, frame_id: str):
        require_release(release_id)
        result = repository.get_frame(release_id, frame_id)
        if result is None:
            abort(404, description="Frame not found.")
        return jsonify(result)

    @server.get("/media/<release_id>/<frame_id>")
    def media(release_id: str, frame_id: str):
        release = require_release(release_id)
        record = repository.get_frame(release_id, frame_id)
        if record is None:
            abort(404, description="Frame not found.")
        try:
            path = resolve_frame_path(Path(release["dataset_root"]), record["relative_path"])
        except (FileNotFoundError, ValueError, OSError):
            abort(404, description="The source image is unavailable.")
        mime = {
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
            ".webp": "image/webp",
        }[path.suffix.lower()]
        return send_file(path, mimetype=mime, conditional=True, max_age=3600)

    register_callbacks(application, repository)
    register_review_callbacks(application, repository, review_repository, brief_repository)
    register_review_routes(server, review_repository)
    register_delivery_callbacks(
        application, repository, review_repository, brief_repository, release_comparison
    )
    register_delivery_routes(
        server, repository, brief_repository, release_comparison, brief_exporter
    )
    return application
