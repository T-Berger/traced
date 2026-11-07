"""Load the explicitly synthetic Harbor investigation used by the review demo."""

from importlib.resources import files

from .contracts import EvidenceBundle


def load_demo_bundle() -> EvidenceBundle:
    raw = files(__package__).joinpath("data/harbor.json").read_text(encoding="utf-8")
    return EvidenceBundle.model_validate_json(raw)
