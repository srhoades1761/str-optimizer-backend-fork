from brief.analysis import (compute_facts, next_tier, rank_moves, rating_after, recovery_after, reviews_to_reach)

def brute(r, n, t):
    x = 0
    while (r * n + 5 * x) / (n + x) < t - 1e-9:
        x += 1
    return x

def test_rating_math_matches_brute_force():
    for r, n, t in [(4.87, 134, 4.90), (4.87, 129, 4.90), (4.87, 129, 4.95), (4.6, 20, 4.7), (4.92, 400, 4.95)]:
        assert reviews_to_reach(r, n, t) == brute(r, n, t)
    assert reviews_to_reach(4.87, 134, 4.90) == 41
    assert reviews_to_reach(4.87, 129, 4.95) == 207
    assert reviews_to_reach(4.9, 10, 5.0) is None
    assert reviews_to_reach(4.95, 10, 4.9) == 0

def test_four_star_cost():
    assert round(rating_after(4.87, 134, 4), 3) == 4.864
    assert recovery_after(4.87, 134) == 7

def test_next_tier():
    assert next_tier(4.87) == 4.9 and next_tier(4.9) == 4.95 and next_tier(4.99) is None

def test_facts_for_hermosa(listing):
    f = compute_facts(listing)
    assert f.lead_pair == ("Location", "Walkability") and f.lead_count == 153 and f.theme_total == 356
    assert f.lead_share == 43
    assert "Location" not in f.title_missing          # "Downtown" counts as location
    assert "Walkability" in f.title_missing
    assert [k for k, _ in f.weak] == ["Cleanliness", "Value"] and f.weak[0][1] == 4.73
    assert f.title_len == 50

def test_moves_ranked(listing):
    moves = rank_moves(listing, compute_facts(listing))
    assert 1 <= len(moves) <= 3
    assert moves[0].key == "title" and "walkability" in moves[0].action
    assert "153 of 356" in moves[0].why
    assert moves[1].key == "subscore:Cleanliness" and "tied for" in moves[1].why and "7 five-stars" in moves[1].why
