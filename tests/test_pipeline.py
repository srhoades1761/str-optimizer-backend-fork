import datetime as dt
from brief.pipeline import build_context, property_name, render
from test_market import stats
from brief.market import analyze_market

def test_property_name():
    assert property_name("Cozy Downtown Cottage | Hot Tub + Family-Friendly!") == "Cozy Downtown Cottage"

def test_render_without_market(listing):
    ctx = build_context(listing, "host@example.com", today=dt.date(2026, 10, 15), unsubscribe_url="https://x/u")
    html = render(ctx)
    assert "{{" not in html and "{%" not in html
    assert "Cozy Downtown Cottage" in html and "Your first listing brief" in html
    assert "Market analysis" not in html and "market section is left out" in html
    assert "Exact text in Ready to paste" not in html      # no LLM, so no paste promise
    assert "Ready to paste" not in html and "04 &middot; What this brief" in html
    assert "41" in html and "4.73" in html
    assert "host%40example.com" in html                     # portal prefilled_email is encoded

def test_render_with_market(listing):
    m = analyze_market(stats(), stats(listings=135), 2, dt.date(2026, 10, 15), "Market data: AirROI estimates.")
    ctx = build_context(listing, "h@example.com", today=dt.date(2026, 10, 15), market=m)
    html = render(ctx)
    assert "Market analysis" in html and "2-BR homes" in html and "AirROI" in html
    assert any(x["action"].startswith("Open November") for x in ctx["moves"])
    assert len(ctx["moves"]) == 3

def test_llm_copy_is_validated_and_rendered(listing, monkeypatch):
    import brief.writer as w
    monkeypatch.setattr(w, "_call_llm", lambda prompt: {
        "headline": "Your guests keep praising the walk to the springs, and your title never says it.",
        "scorecard_lede": "Three scores are perfect; cleanliness and value hold you back.",
        "feedback_lede": "Location and walkability lead everything guests write.",
        "title": "Walk to Hot Springs & Riverwalk · Private Hot Tub",
        "opener": "Walk five minutes to the hot springs, the Riverwalk and downtown coffee, then soak in your own hot tub.",
    })
    ctx = build_context(listing, "h@example.com", today=dt.date(2026, 10, 15))
    html = render(ctx)
    assert "Walk to Hot Springs &amp; Riverwalk · Private Hot Tub" in html
    assert "49 of 50 characters" in html and "Exact text in Ready to paste" in html

def test_llm_invented_numbers_fall_back(listing, monkeypatch):
    import brief.writer as w
    monkeypatch.setattr(w, "_call_llm", lambda prompt: {"headline": "Hosts who fix this earn 38% more.", "title": "x" * 60})
    ctx = build_context(listing, "h@example.com", today=dt.date(2026, 10, 15))
    assert "38%" not in ctx["headline"] and not ctx["paste"]
