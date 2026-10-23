import datetime as dt, pytest
from brief.market import MarketDataError, MarketStats, Month, _monthly_rows, analyze_market, classify_season, upcoming_dates

OCC = [.30, .33, .447, .268, .29, .34, .37, .42, .33, .31, .29, .41]
ADR = [290, 295, 319, 264, 270, 285, 300, 310, 290, 280, 272, 330]

def stats(name="Pagosa Springs", listings=854):
    return MarketStats(name, [Month(i + 1, o, a, o * 30 * a) for i, (o, a) in enumerate(zip(OCC, ADR))], listings, "TTM")

def test_season_has_three_peak_three_slow():
    lv = classify_season(stats().months)
    assert sorted(m for m, v in lv.items() if v == "peak") == [3, 8, 12]
    assert sorted(m for m, v in lv.items() if v == "slow") == [4, 5, 11]

def test_slow_next_month_drives_fill_move():
    mf = analyze_market(stats(), stats(listings=135), 2, dt.date(2026, 10, 15), "src")
    assert mf.action.startswith("Open November") and "slowest" in mf.lede
    assert "December is a peak month" in mf.so_what
    assert mf.columns == ["Whole market", "2-BR homes"] and len(mf.rows[0]["values"]) == 2
    assert [m["name"] for m in mf.season] == list("JFMAMJJASOND") and mf.season[9]["now"]
    assert any("Thanksgiving" in e["what"] and e["when"] == "Nov 26" for e in mf.coming_up)

def test_no_cohort_column_when_missing():
    mf = analyze_market(stats(), None, None, dt.date(2026, 6, 1), "src")
    assert mf.columns == ["Whole market"] and all(len(r["values"]) == 1 for r in mf.rows)

def test_monthly_rows_accepts_record_list_and_percentages():
    rows = [{"date": f"2025-{m:02d}", "occupancy": 33.0, "average_daily_rate": 290, "revenue": 3000} for m in range(1, 13)]
    out = _monthly_rows({"results": rows})
    assert len(out) == 12 and out[0].occupancy == pytest.approx(0.33)

def test_monthly_rows_accepts_parallel_series():
    ser = lambda v: [{"date": f"2025-{m:02d}-01", "value": v} for m in range(1, 13)]
    out = _monthly_rows({"occupancy": ser(0.4), "average_daily_rate": ser(250), "revenue": ser(3000)})
    assert len(out) == 12 and out[5].adr == 250

def test_unknown_schema_withholds_instead_of_guessing():
    with pytest.raises(MarketDataError):
        _monthly_rows({"results": [{"date": "2025-01", "occupancy": .3, "adr": 200}]})
    with pytest.raises(MarketDataError):
        _monthly_rows({"something": "else"})

def test_upcoming_dates_window():
    d = dt.date(2026, 10, 15)
    assert [e["what"] for e in upcoming_dates(d, 60)] == ["Thanksgiving."]
    assert [e["what"] for e in upcoming_dates(d, 90)] == ["Thanksgiving.", "Christmas through New Year's."]
