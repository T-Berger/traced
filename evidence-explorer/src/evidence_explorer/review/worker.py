"""Bounded review worker; no network requests occur inside SQLite transactions."""

from __future__ import annotations

import logging
from threading import Event, Thread

from .analysis import challenge_finding
from .models import ReviewResult
from .storage import ReviewRepository
from .verifier import (
    PROMPT_VERSION,
    RETRIEVAL_VERSION,
    CheckError,
    OllamaVerifier,
    OpenRouterVerifier,
)

logger = logging.getLogger(__name__)


class ReviewWorker:
    def __init__(self, repository: ReviewRepository):
        self.repository = repository

    def run_once(self) -> bool:
        job = self.repository.claim_job_state()
        if job is None:
            return False
        heartbeat_stop = Event()

        def renew_lease() -> None:
            while not heartbeat_stop.wait(30):
                try:
                    if not self.repository.heartbeat_job(job.id, job.attempts):
                        return
                except Exception:
                    logger.warning("Could not renew lease for review job %s", job.id)

        heartbeat = Thread(target=renew_lease, daemon=True)
        heartbeat.start()
        verifier = None
        try:
            payload = job.payload
            bundle, fingerprint = self.repository.get_bundle_snapshot(payload.case_id)
            if fingerprint != payload.case_fingerprint:
                raise ValueError("Investigation changed after queueing; submit a fresh challenge")
            if payload.mode in {"ollama", "openrouter"}:
                if (
                    not payload.model
                    or payload.prompt_version != PROMPT_VERSION
                    or payload.retrieval_version != RETRIEVAL_VERSION
                ):
                    raise CheckError(
                        "Review configuration changed or was not pinned. Submit a new challenge."
                    )
                verifier = (
                    OllamaVerifier.from_env(payload.model, model_identity=payload.model_identity)
                    if payload.mode == "ollama"
                    else OpenRouterVerifier.from_env(payload.model)
                )
            result = challenge_finding(
                bundle,
                payload.finding_id,
                excluded_ids=list(payload.excluded_record_ids),
                mode=payload.mode,
                verifier=verifier,
                bundle_fingerprint=payload.case_fingerprint,
            )
            validated = ReviewResult.model_validate(result)
            self.repository.finish_job(
                job.id,
                validated.model_dump(mode="json", exclude_unset=True),
                expected_attempt=job.attempts,
            )
        except Exception as exc:
            # User-readable bounded errors; never log source text, request headers, or keys.
            logger.warning("Review job %s failed: %s", job.id, type(exc).__name__)
            try:
                self.repository.fail_job(
                    job.id,
                    str(exc)[:1000] or type(exc).__name__,
                    expected_attempt=job.attempts,
                )
            except ValueError:
                logger.warning("Job %s was reclaimed; old attempt left untouched", job.id)
        finally:
            if verifier is not None and hasattr(verifier, "close"):
                verifier.close()
            heartbeat_stop.set()
            heartbeat.join(timeout=1)
        return True

    def run(self, stop: Event | None = None) -> None:
        stop = stop or Event()
        while not stop.is_set():
            try:
                if not self.run_once():
                    stop.wait(0.75)
            except Exception:
                logger.exception("Review worker iteration failed")
                stop.wait(2)
