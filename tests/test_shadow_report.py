import json

from poly_arb_bot.shadow_report import build_report


def test_shadow_report_aggregates_reasons_and_percentiles(tmp_path):
    path = tmp_path / "audit.jsonl"
    rows = [
        {"event_type": "shadow_eval", "market_id": "m1", "reason": "no_edge", "fok": True, "source_age_ms": 10},
        {"event_type": "shadow_eval", "market_id": "m1", "reason": "books_not_synced", "fok": False, "source_age_ms": 30},
        {"event_type": "shadow_opportunity", "market_id": "m1", "duration_ms": 25},
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
    report = build_report(path)
    assert report["evaluations"] == 2
    assert report["fok_passed"] == 1
    assert report["accepts"] == 1
    assert report["rejection_reasons"] == {"no_edge": 1, "books_not_synced": 1}
    assert report["source_age_ms"]["p95"] == 30
