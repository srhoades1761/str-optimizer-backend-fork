"""All arithmetic in the brief lives here. No LLM touches these numbers.

Every function is pure and unit-tested (tests/test_analysis.py).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .listing import Listing
from .market import MarketFacts

RATING_TIERS = [4.5, 4.6, 4.7, 4.8, 4.85, 4.9, 4.95, 5.0]
AIRBNB_TITLE_LIMIT = 50

# Words that show a theme is already present in a title or opening line.
THEME_KEYWORDS = {
    "Location": ["downtown", "location", "central", "heart of", "steps", "minutes", "near", "close to"],
    "Walkability": ["walk", "steps", "stroll"],
    "Nearby": ["near", "close to", "steps", "minutes"],
    "Hot tub": ["hot tub", "spa", "jacuzzi", "hottub"],
    "View": ["view"],
    "Views": ["view"],
    "River": ["river", "riverwalk", "creek"],
    "Lake": ["lake"],
    "Beach": ["beach", "ocean", "oceanfront"],
    "Skiing": ["ski", "slopes"],
    "Family": ["family", "kids"],
    "Kitchen": ["kitchen", "chef"],
    "Pool": ["pool"],
}

SUBSCORE_FIXES = {
    "Cleanliness": ("Tighten your turnover checklist before the next busy stretch.",
                    "Add the spots guests notice first: hot tub or shower drains, under beds, upstairs bathrooms."),
    "Value": ("Add one visible extra guests will notice at check-in.",
              "Value scores move when guests feel they got more than the price. A welcome basket or local guide is cheap."),
    "Accuracy": ("Make your room names and photos match what guests find.",
                 "Accuracy drops when a room or amenity is described more generously than it looks."),
    "Check-in": ("Send arrival instructions 48 hours ahead, with a photo of the door.",
                 "Check-in scores fall when guests have to search for the entrance or the code."),
    "Communication": ("Answer guest messages within an hour during the day.",
                      "Communication is the easiest subscore to lift with a saved-replies list."),
    "Location": ("Describe the neighborhood plainly: parking, noise, and real distances.",
                 "Location scores drop when the surroundings surprise guests."),
}


def reviews_to_reach(rating: float, n: int, target: float) -> int | None:
    """Consecutive 5-star reviews needed to lift `rating` over `n` reviews to `target`."""
    if target >= 5.0 and rating < 5.0:
        return None
    if rating >= target:
        return 0
    # (rating*n + 5x) / (n + x) >= target  ->  x >= n(target - rating) / (5 - target)
    x = n * (target - rating) / (5 - target)
    xi = int(x)
    return xi if xi >= x - 1e-9 else xi + 1


def rating_after(rating: float, n: int, stars: float) -> float:
    return (rating * n + stars) / (n + 1)


def recovery_after(rating: float, n: int, stars: float = 4) -> int:
    """Five-star reviews needed to climb back to `rating` after one review of `stars`."""
    return reviews_to_reach(rating_after(rating, n, stars), n + 1, rating) or 0


def next_tier(rating: float) -> float | None:
    for t in RATING_TIERS:
        if t > rating + 1e-9 and t < 5.0:
            return t
    return None


def mentions(text: str, theme: str) -> bool:
    t = (text or "").lower()
    words = THEME_KEYWORDS.get(theme, [theme.lower()])
    return any(re.search(r"\b" + re.escape(w), t) for w in words)


@dataclass
class Facts:
    rating: float
    review_count: int
    tier: float | None
    to_tier: int | None
    after_four_star: float
    recover: int
    weak: list[tuple[str, float]]          # subscores below overall, weakest first
    theme_total: int
    top_themes: list[tuple[str, int]]
    lead_pair: tuple[str, str] | None      # the two leading themes when they're a pair worth combining
    lead_count: int
    lead_share: int                        # percent, rounded
    title_missing: list[str]               # top themes the title doesn't mention
    title_len: int
    opener: str
    opener_missing: list[str]


def compute_facts(l: Listing) -> Facts:
    rating = l.rating or 0.0
    n = l.review_count
    tier = next_tier(rating)
    to_tier = reviews_to_reach(rating, n, tier) if tier else None
    scores = l.subscores_precise or l.subscores
    weak = sorted(((k, v) for k, v in scores.items() if v < rating - 1e-9), key=lambda kv: (kv[1], kv[0]))
    total = sum(c for _, c in l.themes)
    top = l.themes[:5]
    lead = top[:2] if len(top) >= 2 else top[:1]
    lead_count = sum(c for _, c in lead)
    share = round(lead_count / total * 100) if total else 0
    title_missing = [t for t, _ in top[:3] if not mentions(l.title, t)]
    opener = re.split(r"(?<=[.!?])\s", l.description.strip(), maxsplit=1)[0] if l.description else ""
    opener_missing = [t for t, _ in top[:2] if not mentions(opener, t)]
    return Facts(
        rating=rating, review_count=n, tier=tier, to_tier=to_tier,
        after_four_star=rating_after(rating, n, 4), recover=recovery_after(rating, n),
        weak=weak, theme_total=total, top_themes=top,
        lead_pair=(lead[0][0], lead[1][0]) if len(lead) == 2 else None,
        lead_count=lead_count, lead_share=share, title_missing=title_missing,
        title_len=len(l.title), opener=opener, opener_missing=opener_missing,
    )


@dataclass
class Move:
    priority: int
    action: str
    why: str
    key: str  # stable id, used for month-two progress tracking


def theme_phrase(f: Facts) -> str:
    if f.lead_pair:
        return f"{f.lead_pair[0].lower()} or {f.lead_pair[1].lower()}"
    return f.top_themes[0][0].lower() if f.top_themes else ""


def rank_moves(l: Listing, f: Facts, market: MarketFacts | None = None) -> list[Move]:
    moves: list[Move] = []
    lead = [t for t, _ in f.top_themes[:2] if t in f.title_missing]
    if lead and f.lead_share >= 20:
        moves.append(Move(90, f"Work {lead[0].lower()} into your title.",
                          f"{f.lead_count} of {f.theme_total} guest mentions are {theme_phrase(f)}, "
                          "and your title doesn't say it. Exact text in Ready to paste.", "title"))
    if f.weak:
        name, val = f.weak[0]
        action, detail = SUBSCORE_FIXES.get(name, (f"Work on {name.lower()}.", ""))
        tie = " tied for" if len(f.weak) > 1 and abs(f.weak[1][1] - val) < 0.005 else ""
        moves.append(Move(80, action,
                          f"{name} is {val:.2f},{tie} your lowest score. One 4-star review costs you "
                          f"{f.recover} five-stars to recover. {detail}".strip(), f"subscore:{name}"))
    if market is not None and market.action:
        moves.append(Move(70, market.action, market.action_why, "market"))
    if f.opener and f.opener_missing and f.top_themes:
        moves.append(Move(60, "Rewrite the first line of your description.",
                          f"It's what shows in search, and it doesn't mention {f.opener_missing[0].lower()} yet. "
                          "Exact text in Ready to paste.", "opener"))
    for theme, count in f.top_themes[1:4]:
        if theme in f.title_missing and theme not in ("Location", "Walkability", "Nearby", "Comfort", "Hospitality"):
            moves.append(Move(50, f"Put the {theme.lower()} in your first three photos.",
                              f"Guests mention it {count} times. It should be the first thing people see.",
                              f"photo:{theme}"))
            break
    return sorted(moves, key=lambda m: -m.priority)[:3]

