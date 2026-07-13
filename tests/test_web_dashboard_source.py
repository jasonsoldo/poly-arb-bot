from pathlib import Path


def test_dashboard_uses_paired_lock_execution_and_health_fields():
    source = Path("web/index.html").read_text(encoding="utf-8")
    assert "BTC PAIRED LOCK / SHADOW" in source
    assert "EXPECTED EXEC VALUE" in source
    assert "CLOB READY" in source
    assert "SUBSCRIPTION GEN" in source
    assert "EXECUTION STATE" in source
    assert "expected_execution_value" in source
    assert "BTC UP / DOWN SHADOW SCALPER" not in source
