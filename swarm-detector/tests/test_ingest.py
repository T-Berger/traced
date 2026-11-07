import json
import zipfile
from copy import deepcopy
from pathlib import Path

import pytest

from swarm_detector.detector import analyze
from swarm_detector.ingest import dataset_readiness, inspect_archive, load_jsonl
from swarm_detector.sample_data import build_sample
from swarm_detector.storage import Store


def write_records(tmp_path, stable=False):
    records = [
        {
            "record_type": "metadata",
            "dataset_id": "import-test",
            "manifest": {
                "identity_stable_across_channels": stable,
                "timestamp_precision_seconds": 1,
            },
        },
        {"record_type": "channel", "channel_key": "c"},
        {
            "record_type": "post",
            "post_key": "p",
            "channel_key": "c",
            "timestamp_utc": "2026-10-14T08:00:00Z",
        },
        {
            "record_type": "message",
            "message_key": "m",
            "post_key": "p",
            "channel_key": "c",
            "account_key": "a",
            "timestamp_utc": "2026-10-14T08:01:00Z",
        },
    ]
    path = tmp_path / "input.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in records))
    return path, records


def test_unverified_cross_channel_ids_are_scoped(tmp_path):
    path, _ = write_records(tmp_path)
    dataset = load_jsonl(path)
    assert json.loads(dataset["messages"][0]["account_key"]) == ["c", "a"]
    assert dataset["manifest"]["limitations"]
    assert dataset["channels"][0]["category"] == "unknown"


def test_verified_ids_are_preserved_and_sources_resolve(tmp_path):
    path, _ = write_records(tmp_path, stable=True)
    dataset = load_jsonl(path)
    assert dataset["messages"][0]["account_key"] == "a"
    assert dataset["messages"][0]["source_ref"].endswith("#message=m")


@pytest.mark.parametrize(
    "change", ["missing_parent", "naive_timestamp", "before_parent", "conflicting_duplicate"]
)
def test_invalid_source_contract_is_rejected(tmp_path, change):
    path, records = write_records(tmp_path)
    if change == "missing_parent":
        records[-1]["post_key"] = "missing"
    elif change == "naive_timestamp":
        records[-1]["timestamp_utc"] = "2026-10-14T08:01:00"
    elif change == "before_parent":
        records[-1]["timestamp_utc"] = "2026-10-13T08:01:00Z"
    else:
        records.append({**records[-1], "text_original": "Conflicting content"})
    path.write_text("\n".join(json.dumps(row) for row in records))
    with pytest.raises(ValueError):
        load_jsonl(path)


def test_archive_inventory_never_extracts_and_rejects_partial(tmp_path):
    archive = tmp_path / "data.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("../should-not-extract.json", '{"data": []}')
    assert inspect_archive(archive)["member_count"] == 1
    assert not (tmp_path.parent / "should-not-extract.json").exists()
    with pytest.raises(ValueError, match="completed"):
        inspect_archive(tmp_path / "Unconfirmed.crdownload")


@pytest.mark.parametrize(
    ("record_index", "field", "value"),
    [
        (0, "manifest", None),
        (0, "label", 42),
        (1, "name", []),
        (2, "channel_key", None),
        (2, "title", {}),
        (3, "post_key", []),
        (3, "timestamp_utc", None),
        (3, "sender_type", 1),
    ],
)
def test_malformed_fields_fail_at_the_input_boundary(tmp_path, record_index, field, value):
    path, records = write_records(tmp_path)
    records[record_index][field] = value
    path.write_text("\n".join(json.dumps(row) for row in records))
    with pytest.raises(ValueError, match=rf"line {record_index + 1}.*{field}"):
        load_jsonl(path)


@pytest.mark.parametrize("record_index,field", [(2, "timestamp_utc"), (3, "post_key")])
def test_missing_required_fields_are_validation_errors(tmp_path, record_index, field):
    path, records = write_records(tmp_path)
    records[record_index].pop(field)
    path.write_text("\n".join(json.dumps(row) for row in records))
    with pytest.raises(ValueError, match=rf"line {record_index + 1}.*{field}"):
        load_jsonl(path)


def test_empty_metadata_record_still_counts_toward_duplicate_limit(tmp_path):
    path, records = write_records(tmp_path)
    records.insert(0, {"record_type": "metadata"})
    path.write_text("\n".join(json.dumps(row) for row in records))
    with pytest.raises(ValueError, match="Only one metadata"):
        load_jsonl(path)


def test_import_preserves_supplied_source_limitations(tmp_path):
    path, records = write_records(tmp_path)
    records[0]["manifest"]["limitations"] = ["The source archive is incomplete."]
    path.write_text("\n".join(json.dumps(row) for row in records))
    assert load_jsonl(path)["manifest"]["limitations"] == [
        "The source archive is incomplete.",
        "Account IDs are scoped per channel; cross-channel identity is unverified.",
    ]


@pytest.mark.parametrize("value", ["true", 1, None])
def test_cross_channel_identity_flag_cannot_silently_coerce_invalid_types(tmp_path, value):
    path, records = write_records(tmp_path)
    records[0]["manifest"]["identity_stable_across_channels"] = value
    path.write_text("\n".join(json.dumps(row) for row in records))
    with pytest.raises(ValueError, match="identity_stable_across_channels"):
        load_jsonl(path)


@pytest.fixture(scope="module")
def sample_readiness():
    dataset = build_sample()
    run = analyze(dataset)
    return dataset, run, dataset_readiness(dataset, run)


def checks_by_key(readiness):
    return {check["key"]: check for check in readiness["checks"]}


def test_readiness_uses_observed_records_and_marks_sample_as_synthetic(sample_readiness):
    dataset, run, readiness = sample_readiness
    assert readiness["dataset_id"] == dataset["dataset_id"]
    assert readiness["run_id"] == run["run_id"]
    assert readiness["synthetic"] is True
    assert "synthetic" in readiness["label"].lower()
    assert readiness["post_count"] == 89
    assert readiness["message_count"] == 1137
    assert readiness["channel_count"] == 3
    assert readiness["account_count"] == len({m["account_key"] for m in dataset["messages"]})
    assert readiness["observation_start"] == "2026-10-14T08:00:00+00:00"
    assert readiness["observation_end"] == "2026-10-20T11:08:33+00:00"
    assert readiness["capabilities"] == {
        "cross_channel_identity": True,
        "timestamp_precision_seconds": 1,
    }
    checks = checks_by_key(readiness)
    assert set(checks) == {
        "identities",
        "time_precision",
        "thread_mapping",
        "source_refs",
        "coverage",
        "baseline",
    }
    assert checks["thread_mapping"]["status"] == "verified"
    assert checks["source_refs"]["status"] == "verified"
    assert checks["coverage"]["status"] == "unknown"
    assert checks["baseline"]["status"] == "limited"
    assert "6 of 25" in checks["baseline"]["detail"]
    assert any("synthetic" in note.lower() for note in readiness["limitations"])


def test_readiness_is_pure_and_preserves_source_limitations(sample_readiness):
    dataset, run, _ = sample_readiness
    dataset = deepcopy(dataset)
    dataset["manifest"]["limitations"] = ["Declared source window excludes two days."]
    before, run_before = deepcopy(dataset), deepcopy(run)
    readiness = dataset_readiness(dataset, run)
    assert dataset == before
    assert run == run_before
    assert "Declared source window excludes two days." in readiness["limitations"]


def test_import_counts_never_become_archive_denominators(sample_readiness):
    dataset, run, _ = sample_readiness
    dataset = deepcopy(dataset)
    dataset["manifest"]["record_counts"] = {"messages": 194_000}
    dataset["manifest"]["messages"] = 194_000
    dataset["manifest"]["available_messages"] = 194_000
    readiness = dataset_readiness(dataset, run)
    assert readiness["coverage"]["available_messages"] is None
    assert readiness["coverage"]["imported_messages"] == len(dataset["messages"])


def test_explicit_coverage_remains_scoped_and_declared(sample_readiness):
    dataset, run, _ = sample_readiness
    dataset = deepcopy(dataset)
    dataset["manifest"]["source_coverage"] = {
        "available_messages": 2000,
        "scope": "selected fictional interval, not the complete archive",
        "evidence": "Synthetic fixture declaration for this test only.",
    }
    readiness = dataset_readiness(dataset, run)
    assert readiness["coverage"]["available_messages"] == 2000
    assert readiness["coverage"]["denominator_scope"].startswith("selected fictional")
    assert checks_by_key(readiness)["coverage"]["status"] == "limited"
    dataset["manifest"]["source_coverage"]["available_messages"] = 2
    assert dataset_readiness(dataset, run)["coverage"]["available_messages"] is None


def test_missing_precision_does_not_make_default_look_source_verified(tmp_path):
    path, records = write_records(tmp_path)
    records[0]["manifest"].pop("timestamp_precision_seconds")
    path.write_text("\n".join(json.dumps(row) for row in records))
    dataset = load_jsonl(path)
    assert dataset["manifest"]["timestamp_precision_seconds"] == 60
    assert dataset["manifest"]["timestamp_precision_declared"] is False
    readiness = dataset_readiness(dataset, None)
    assert readiness["capabilities"]["cross_channel_identity"] is False
    assert readiness["capabilities"]["timestamp_precision_seconds"] == 60
    assert checks_by_key(readiness)["time_precision"]["status"] == "unknown"
    assert "assumption" in checks_by_key(readiness)["time_precision"]["detail"]


def test_readiness_exposes_missing_and_inconsistent_evidence(sample_readiness):
    dataset, run, _ = sample_readiness
    dataset, run = deepcopy(dataset), deepcopy(run)
    dataset["messages"][0].update(
        source_ref="", post_key="unresolved", timestamp_utc="2026-10-14T08:01:00"
    )
    run["pairs"][0]["events"][0]["message_a"] = "not-in-import"
    checks = checks_by_key(dataset_readiness(dataset, run))
    assert checks["time_precision"]["status"] == "limited"
    assert checks["thread_mapping"]["status"] == "limited"
    assert checks["source_refs"]["status"] == "limited"


def test_no_controls_and_wrong_dataset_cannot_look_baseline_verified(sample_readiness):
    dataset, run, _ = sample_readiness
    readiness = dataset_readiness(dataset, None)
    assert checks_by_key(readiness)["baseline"]["status"] == "unknown"
    run = {**run, "dataset_id": "a-different-source"}
    readiness = dataset_readiness(dataset, run)
    assert checks_by_key(readiness)["baseline"]["status"] == "limited"
    assert "different dataset" in checks_by_key(readiness)["baseline"]["detail"]


def test_original_ukrainian_russian_text_round_trips_without_translation(tmp_path):
    path = Path(__file__).with_name("fixtures") / "ukrainian_russian_context.jsonl"
    source_records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    expected = {
        row["message_key"]: row["text_original"]
        for row in source_records
        if row["record_type"] == "message"
    }
    dataset = load_jsonl(path)
    store = Store(tmp_path / "original-language.db")
    store.put_dataset(dataset)
    recovered = store.get_dataset(dataset["dataset_id"])
    actual = {m["message_key"]: m["text_original"] for m in recovered["messages"]}
    assert actual == expected
    serialized = json.dumps(recovered, ensure_ascii=False)
    assert "Я критикую рішення уряду" in serialized
    assert "Я с этим не согласен" in serialized
    assert {
        m["message_key"]: m["text_original"] for m in json.loads(serialized)["messages"]
    } == expected
    result = analyze(recovered, {"randomizations": 2})
    assert result["groups"] == []
    readiness = dataset_readiness(recovered, result)
    assert readiness["synthetic"] is True
    assert readiness["capabilities"]["cross_channel_identity"] is False
