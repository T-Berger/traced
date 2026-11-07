from copy import deepcopy

import pytest
from pydantic import ValidationError

from swarm_detector.detector import analyze
from swarm_detector.models import AnalysisRun, Dataset, Review
from swarm_detector.sample_data import build_sample
from swarm_detector.storage import Store


def test_validated_models_preserve_wire_fields_extensions_and_source_language():
    wire = build_sample()
    wire["source_extension"] = {"reviewed_by": "Analyst"}
    wire["messages"][0]["text_original"] = "Я критикую рішення уряду. Я с этим не согласен."
    record = Dataset.model_validate(wire)
    assert record.to_record() == wire
    assert record.messages[0].text_original == wire["messages"][0]["text_original"]
    with pytest.raises(ValidationError, match="frozen"):
        record.dataset_id = "changed"
    result = analyze(wire, {"randomizations": 0})
    assert AnalysisRun.model_validate(result).to_record() == result


@pytest.mark.parametrize(
    "change",
    [
        "duplicate-message",
        "missing-post",
        "wrong-channel",
        "naive-time",
        "before-parent",
        "bad-urls",
    ],
)
def test_invalid_source_models_are_rejected_before_any_write(tmp_path, change):
    wire = build_sample()
    if change == "duplicate-message":
        wire["messages"].append(deepcopy(wire["messages"][0]))
    elif change == "missing-post":
        wire["messages"][0]["post_key"] = "missing"
    elif change == "wrong-channel":
        wire["messages"][0]["channel_key"] = "wrong-channel"
    elif change == "naive-time":
        wire["messages"][0]["timestamp_utc"] = "2026-10-14T08:00:00"
    elif change == "before-parent":
        wire["messages"][0]["timestamp_utc"] = "2026-10-13T08:00:00Z"
    else:
        wire["messages"][0]["urls"] = [1]
    store = Store(tmp_path / "models.db")
    with pytest.raises(ValidationError):
        store.put_dataset(wire)
    assert store.datasets.info() is None
    with store.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM datasets").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM messages_fts").fetchone()[0] == 0


def test_review_model_enforces_narrative_evidence_invariant():
    with pytest.raises(ValidationError, match="supporting comments"):
        Review(
            run_id="run",
            case_id="case",
            reviewer="Analyst",
            decision="Insufficient evidence",
            reason="Needs more source context.",
            claim="An unsupported narrative assertion",
        )


def test_run_model_rejects_coercible_but_invalid_detector_settings():
    result = analyze(build_sample(), {"randomizations": 0})
    result["config"]["randomizations"] = True
    with pytest.raises(ValidationError, match="randomizations"):
        AnalysisRun.model_validate(result)
