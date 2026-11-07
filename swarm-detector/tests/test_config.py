import pytest

from swarm_detector.config import DetectionConfig, positive_number
from swarm_detector.detector import analyze


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("window_seconds", True),
        ("window_seconds", float("nan")),
        ("window_seconds", float("inf")),
        ("min_families", 1.5),
        ("min_families", False),
        ("max_pairs_per_post", 0),
        ("randomizations", True),
        ("randomizations", -1),
        ("swaps_per_slot", 0.5),
        ("seed", "42"),
        ("seed", True),
    ],
)
def test_invalid_detector_settings_fail_before_analysis(name, value):
    with pytest.raises(ValueError, match=name):
        analyze({}, {name: value})


def test_unknown_detector_settings_are_rejected_instead_of_silently_ignored():
    with pytest.raises(ValueError, match="Unknown or invalid"):
        DetectionConfig.from_mapping({"randomization": 100})


@pytest.mark.parametrize("value", [True, False, float("nan"), float("inf"), 0, -1, "1"])
def test_timestamp_precision_must_support_finite_timing_arithmetic(value):
    with pytest.raises(ValueError, match="timestamp_precision_seconds"):
        positive_number(value, "timestamp_precision_seconds")


def test_zero_controls_remain_an_explicit_supported_setting():
    result = analyze({}, {"randomizations": 0})
    assert result["config"]["randomizations"] == 0
    assert result["groups"] == []
