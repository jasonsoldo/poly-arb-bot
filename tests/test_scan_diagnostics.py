from pathlib import Path


SOURCE = Path("poly_arb_bot/cli.py").read_text(encoding="utf-8")


def test_scan_reports_every_network_stage_and_worker_counts():
    for marker in (
        "GAMMA_SERIES_START", "GAMMA_SERIES_DONE", "GAMMA_EVENTS_START",
        "GAMMA_EVENTS_DONE", "MARKET_PARSE_DONE", "CLOB_VALIDATE_START",
        "CLOB_VALIDATE_DONE", "WRITE_START", "WRITE_DONE",
    ):
        assert marker in SOURCE
    assert "gamma_request_count=" in SOURCE
    assert "clob_request_count=" in SOURCE
    assert "active_workers=" in SOURCE
