"""Shared Ollama HTTP transport, model identity, and structured generation.

Article, review, and claim-check workflows reuse this client. Domain services own
prompts, grounding, and persistence. ``extract_claims`` remains a compatibility
facade for article selection; its policy lives in ``article_extraction``. Calls
never fall back to cloud models or demo evidence.
"""

import math

import httpx
from pydantic import BaseModel

from .article_extraction import ArticleClaimExtractor, ClaimExtractionError
from .claim_selection import CLAIMS_POLICY_VERSION
from .config import Settings
from .models import ClaimInput


class ModelError(RuntimeError):
    """An actionable model/runtime failure suitable for a worker job error."""


class OllamaClient:
    """Own an HTTP client and per-instance model identity and generation metadata.

    Use as a context manager to close the client. Generic generation validates the
    supplied schema; callers still own evidence grounding and result persistence.
    The article extraction compatibility method delegates its stricter grounding.
    """

    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None):
        self.settings = settings
        self.client = httpx.Client(
            base_url=settings.ollama_url,
            timeout=httpx.Timeout(180, connect=3),
            transport=transport,
        )
        self._models: dict[str, str] | None = None
        self.last_generation_metadata: dict = {}
        self.last_claim_extraction_metadata: dict = {}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.client.close()

    def _request(self, method: str, path: str, **kwargs) -> dict:
        try:
            response = self.client.request(method, path, **kwargs)
            response.raise_for_status()
            value = response.json()
            if not isinstance(value, dict):
                raise ValueError("expected a JSON object")
            if value.get("error"):
                raise ModelError(str(value["error"])[:400])
            return value
        except httpx.ConnectError as exc:
            raise ModelError(
                f"Cannot reach Ollama at {self.settings.ollama_url}. Start Ollama, then retry."
            ) from exc
        except httpx.TimeoutException as exc:
            raise ModelError(
                "Ollama timed out. Try a smaller Qwen model or fewer articles."
            ) from exc
        except httpx.HTTPStatusError as exc:
            try:
                body = exc.response.json()
                detail = body.get("error", exc.response.text) if isinstance(body, dict) else body
            except ValueError:
                detail = exc.response.text
            raise ModelError(
                f"Ollama HTTP {exc.response.status_code}: {str(detail)[:400]}"
            ) from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise ModelError(f"Invalid Ollama response: {exc}") from exc

    def _available_models(self) -> dict[str, str]:
        if self._models is None:
            response = self._request("GET", "/api/tags", timeout=3)
            entries = response.get("models")
            if not isinstance(entries, list) or any(
                not isinstance(model, dict) or not isinstance(model.get("name"), str)
                for model in entries
            ):
                raise ModelError("Ollama returned an invalid model list.")
            self._models = {model["name"]: model.get("digest", "unknown") for model in entries}
        return self._models

    def _resolve(self, name: str) -> tuple[str, str]:
        models = self._available_models()
        for candidate in (name, f"{name}:latest"):
            if candidate in models:
                return candidate, models[candidate]
        raise ModelError(f"Model '{name}' is missing. Run: ollama pull {name}")

    def status(self) -> dict:
        try:
            models = self._available_models()
            missing = []
            for name in (self.settings.embedding_model, self.settings.chat_model):
                if name not in models and f"{name}:latest" not in models:
                    missing.append(name)
            return {"available": True, "models": list(models), "missing": missing, "error": None}
        except ModelError as exc:
            return {"available": False, "models": [], "missing": [], "error": str(exc)}

    def model_key(self, kind: str = "embedding") -> str:
        if kind not in {"embedding", "chat", "claims"}:
            raise ValueError("Model kind must be embedding, chat or claims.")
        name = self.settings.embedding_model if kind == "embedding" else self.settings.chat_model
        resolved, digest = self._resolve(name)
        revision = CLAIMS_POLICY_VERSION if kind == "claims" else "v1"
        return f"ollama:{resolved}@{digest}:{revision}"

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        self._resolve(self.settings.embedding_model)
        response = self._request(
            "POST",
            "/api/embed",
            json={"model": self.settings.embedding_model, "input": texts, "truncate": False},
        )
        embeddings = response.get("embeddings")
        if not isinstance(embeddings, list) or len(embeddings) != len(texts):
            raise ModelError("Ollama returned the wrong number of embeddings.")
        dimensions = None
        vectors = []
        for vector in embeddings:
            if not isinstance(vector, list) or not vector:
                raise ModelError("Ollama returned an empty embedding.")
            if dimensions is None:
                dimensions = len(vector)
            if len(vector) != dimensions:
                raise ModelError("Ollama returned inconsistent embedding dimensions.")
            try:
                values = [float(v) for v in vector]
            except (TypeError, ValueError) as exc:
                raise ModelError("Ollama returned a nonnumeric embedding.") from exc
            if not all(math.isfinite(v) for v in values) or not any(values):
                raise ModelError("Ollama returned an invalid embedding.")
            vectors.append(values)
        return vectors

    def generate_structured[Output: BaseModel](
        self,
        messages: list[dict[str, str]],
        schema: type[Output],
        *,
        max_tokens: int,
    ) -> Output:
        """Generate one schema-valid JSON result and record its model metadata.

        Check completion and returned model identity before accepting the response.
        Schema validity alone does not verify citations or establish factual truth;
        those checks belong to the calling workflow.
        """
        resolved_model, _digest = self._resolve(self.settings.chat_model)
        self.last_generation_metadata = {}
        payload = {
            "model": self.settings.chat_model,
            "stream": False,
            "think": False,
            "format": schema.model_json_schema(),
            "options": {"temperature": 0, "num_ctx": 8192, "num_predict": max_tokens},
            "messages": messages,
        }
        response = self._request("POST", "/api/chat", json=payload)
        try:
            if "done" in response and response["done"] is not True:
                raise ValueError("generation did not finish")
            if response.get("done_reason") not in {None, "stop"}:
                raise ValueError("generation ended before a complete result")
            actual_model = response.get("model")
            if actual_model is not None and actual_model not in {
                self.settings.chat_model,
                resolved_model,
            }:
                raise ValueError("response model does not match the requested model")
            message = response["message"]
            if not isinstance(message, dict) or not isinstance(message.get("content"), str):
                raise ValueError("expected a message with JSON text content")
            result = schema.model_validate_json(message["content"])
            self.last_generation_metadata = {
                "requested_model": self.settings.chat_model,
                "actual_model": actual_model or resolved_model,
                "model_reported": actual_model is not None,
                "done_reason": response.get("done_reason"),
            }
            return result
        except (KeyError, TypeError, ValueError) as exc:
            raise ModelError(
                "Ollama returned invalid structured JSON; no result was accepted."
            ) from exc

    def extract_claims(self, article: dict, chunks: list[dict]) -> list[ClaimInput]:
        """Delegate bounded article selection while preserving the client API.

        The domain service retains source wording and permits one correction of
        invalid IDs. Keep its live attempt/rejection metadata on this client for
        existing workers, including failed calls. Pass the bound generator at call
        time so client subclasses and injected test generators remain supported.
        """
        extractor = ArticleClaimExtractor(self.generate_structured)
        self.last_claim_extraction_metadata = extractor.last_metadata
        try:
            return extractor.extract_claims(article, chunks)
        except ClaimExtractionError as exc:
            raise ModelError(str(exc)) from exc
