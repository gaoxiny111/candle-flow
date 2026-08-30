from app.services.fund_flow_client import parse_fflow_klines


def test_parse_fflow_klines():
    rows = [
        "2026-08-28 09:31,-100000000.0,0,0,0,0",
        "2026-08-28 10:30,250000000.5,0,0,0,0",
        "bad-row",
    ]
    points = parse_fflow_klines(rows)
    assert len(points) == 2
    assert points[0]["time"] == "09:31"
    assert points[0]["date"] == "2026-08-28"
    assert points[0]["value"] == -100000000.0
    assert points[1]["time"] == "10:30"
    assert points[1]["value"] == 250000000.5
