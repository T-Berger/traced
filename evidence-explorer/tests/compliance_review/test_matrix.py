from evidence_explorer.compliance_review.matrix import build_monthly_matrix, fictional_demo


def test_fictional_matrix_links_counts_and_keeps_incomplete_months_unavailable():
    demo = fictional_demo()
    result = build_monthly_matrix(
        demo["records"], demo["annotations"], demo["coverage"], fictional=True
    )
    jan, feb, mar, apr = result["months"]
    assert demo["fictional"] is True
    assert jan["categories"]["disinformation"]["record_ids"] == ["fiction:post:1"]
    assert feb["categories"]["harassment"]["confirmed"] == 1
    assert mar["categories"]["hate_speech"]["proposed"] is None
    assert mar["categories"]["hate_speech"]["observed_proposed"] == 1
    assert apr["categories"]["harassment"]["confirmed"] is None
    assert result["undated_record_ids"] == ["fiction:post:4"]
    assert result["rows"][0]["notice"]["status"] == "documented"
    assert result["rows"][0]["response"]["status"] == "documented"


def test_unresolved_and_model_claims_cannot_count_as_human_review():
    demo = fictional_demo()
    annotations = [
        {
            "record_id": "fiction:post:1",
            "category": "racism",
            "origin": "model",
            "status": "confirmed",
            "quote": "Fictional example",
            "reviewer": "model",
        },
        {
            "record_id": "fiction:post:1",
            "category": "hate_speech",
            "origin": "human",
            "status": "confirmed",
            "quote": "fabricated quotation",
            "reviewer": "person",
        },
        {
            "record_id": "missing",
            "category": "racism",
            "origin": "human",
            "status": "confirmed",
            "quote": "anything",
            "reviewer": "person",
        },
    ]
    records = [dict(item) for item in demo["records"]]
    records[0]["notice"] = {"record_id": "missing", "quote": "made up"}
    result = build_monthly_matrix(records, annotations, demo["coverage"], fictional=True)
    assert len(result["invalid_annotations"]) == 3
    assert result["months"][0]["categories"]["racism"]["confirmed"] == 0
    assert result["rows"][0]["notice"]["status"] == "unknown"


def test_distinct_saved_ids_count_once_per_category_and_allow_overlap():
    demo = fictional_demo()
    a = demo["annotations"][0]
    other = {**a, "category": "racism"}
    result = build_monthly_matrix(demo["records"], [a, a, other], demo["coverage"], fictional=True)
    jan = result["months"][0]["categories"]
    assert jan["disinformation"]["confirmed"] == 1
    assert jan["racism"]["confirmed"] == 1


def test_unknown_or_naive_dates_are_not_guessed():
    records = [{"id": "a", "kind": "post", "text": "A", "published_at": "2026-01-01T10:00:00"}]
    result = build_monthly_matrix(records, [], {})
    assert result["undated_record_ids"] == ["a"]
    assert result["months"] == []


def test_real_projection_does_not_promote_claimed_coverage_or_review():
    demo = fictional_demo()
    result = build_monthly_matrix(demo["records"], demo["annotations"], demo["coverage"])
    january = result["months"][0]
    assert january["coverage"] == "unknown"
    assert january["categories"]["disinformation"]["confirmed"] is None
    assert not result["rows"][0]["categories"]
    assert result["rows"][0]["notice"]["status"] == "unverified"
    assert any("Untrusted review claim" in item["reason"] for item in result["invalid_annotations"])


def test_review_does_not_transfer_to_changed_saved_text():
    demo = fictional_demo()
    demo["records"][0]["text"] += " Changed."
    result = build_monthly_matrix(
        demo["records"], demo["annotations"], demo["coverage"], fictional=True
    )
    assert result["months"][0]["categories"]["disinformation"]["confirmed"] == 0
    assert any("saved text version" in item["reason"] for item in result["invalid_annotations"])


def test_nonpost_and_unknown_kinds_are_not_silently_counted_as_posts():
    records = [
        {"id": "post", "kind": "post", "text": "Post", "published_at": "2026-01-01T00:00:00Z"},
        {"id": "channel", "kind": "telegram_channel", "text": "Channel"},
        {"id": "unknown", "text": "Unknown"},
    ]
    result = build_monthly_matrix(records, [], {})
    assert [row["record_id"] for row in result["rows"]] == ["post"]
    assert result["excluded_records"] == [
        {"record_id": "channel", "kind": "telegram_channel"},
        {"record_id": "unknown", "kind": None},
    ]


def test_post_text_cannot_serve_as_its_own_notice_or_response():
    demo = fictional_demo()
    demo["records"][0]["notice"] = {"record_id": "fiction:post:1", "quote": "Fictional example"}
    result = build_monthly_matrix(
        demo["records"], demo["annotations"], demo["coverage"], fictional=True
    )
    assert result["rows"][0]["notice"]["status"] == "unknown"
