from brief.analysis import compute_facts
from brief.writer import fallback, validate, write_copy

def test_without_llm_uses_fallback_and_no_paste(listing):
    c = write_copy(listing, compute_facts(listing))
    assert c["title"] is None and c["opener"] is None
    assert "43%" in c["headline"] and "location or walkability" in c["headline"]
    assert "Cleanliness and value are holding you below 4.9" in c["scorecard_lede"]
    assert c["scorecard_lede"].startswith("Check-in, communication and location are perfect.")
    assert "doesn't mention walkability yet" in c["headline"]

def test_validation(listing):
    f = compute_facts(listing)
    good = validate({"title": "Walk to Hot Springs & Riverwalk · Private Hot Tub",
                     "opener": "Walk five minutes to the hot springs and the Riverwalk, then come home to your private hot tub.",
                     "headline": "153 of 356 guest mentions are about location or walkability."}, listing, f)
    assert good["title"] and good["opener"] and good["headline"]
    bad = validate({"title": "x" * 51, "headline": "Hosts like you earn 27% more.",
                    "opener": "Cozy cottage."}, listing, f)
    assert bad["title"] is None and bad["headline"] is None and bad["opener"] is None
    # title must actually add a missing theme
    assert validate({"title": "Cozy Downtown Cottage with Hot Tub"}, listing, f)["title"] is None
