import json, pytest
from brief.listing import ListingError, extract_page_data, normalize_url, parse_listing

def test_parses_live_page_shape(listing):
    l = listing
    assert l.listing_id == "50439899" and l.url == "https://www.airbnb.com/rooms/50439899"
    assert l.title == "Cozy Downtown Cottage | Hot Tub + Family-Friendly!"
    assert l.rating == 4.87 and l.review_count == 134
    assert list(l.subscores) == ["Cleanliness", "Accuracy", "Check-in", "Communication", "Location", "Value"]
    assert l.subscores["Cleanliness"] == 4.7 and l.subscores_precise["Cleanliness"] == 4.73
    assert l.themes[:3] == [("Location", 86), ("Walkability", 67), ("Comfort", 32)]
    assert sum(c for _, c in l.themes) == 356
    assert (l.lat, l.lng) == (37.26649, -107.00503)
    assert l.location == "Pagosa Springs, Colorado"
    assert (l.bedrooms, l.beds, l.baths, l.guests) == (2, 4, 1.5, 6)
    assert len(l.description_sections) == 3 and "<br" not in l.description
    assert any("hot tub" in a.lower() for a in l.amenities)
    assert l.guest_favorite and l.missing == []

def test_missing_fields_degrade_not_crash():
    l = parse_listing({"niobeClientData": [["x", {"data": {}}]]}, "https://airbnb.com/rooms/1")
    assert set(l.missing) >= {"title", "rating", "subscores", "review themes", "description"}

@pytest.mark.parametrize("url", ["https://evil.com/rooms/1", "http://169.254.169.254/rooms/1",
                                 "https://www.airbnb.com/s/homes", "ftp://airbnb.com/rooms/1",
                                 "https://airbnb.com.evil.com/rooms/1"])
def test_rejects_non_listing_urls(url):
    with pytest.raises(ListingError):
        normalize_url(url)

def test_extracts_embedded_json(pdp):
    page = '<html><script id="data-deferred-state-0" type="application/json">' + json.dumps(pdp) + "</script></html>"
    assert extract_page_data(page) == pdp
    with pytest.raises(ListingError):
        extract_page_data("<html></html>")
