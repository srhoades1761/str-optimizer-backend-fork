"""Build and send a listing brief.

    listing page ──► Listing ──► Facts (tested math) ──► moves
                                     │                      │
    AirROI ──► MarketFacts ──────────┴──► copy (LLM, validated) ──► template ──► SendGrid
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import re
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .analysis import AIRBNB_TITLE_LIMIT, compute_facts, rank_moves, theme_phrase
from .listing import Listing, read_listing
from .market import AirROI, MarketDataError, analyze_market, fetch_stats
from .writer import write_copy

log = logging.getLogger("brief")
_env = Environment(loader=FileSystemLoader(Path(__file__).parent / "templates"),
                   autoescape=select_autoescape(["html", "j2"]))
SITE = os.environ.get("FRONTEND_URL", "https://optimizemystr.com").rstrip("/")


def property_name(title: str) -> str:
    name = re.split(r"\s[|·\-–—]\s|!", title)[0].strip()
    return name[:40] or "Your listing"


def build_market(l: Listing, today: dt.date, cache=None):
    if l.lat is None or l.lng is None or not os.environ.get("AIRROI_API_KEY"):
        return None
    try:
        whole, cohort = fetch_stats(AirROI(), l.lat, l.lng, l.bedrooms, cache=cache)
        return analyze_market(whole, cohort, l.bedrooms if cohort else None, today,
                              sources="Market data: AirROI estimates, trailing 12 months.")
    except MarketDataError as e:
        log.warning("market data unavailable for %s: %s", l.url, e)
        return None


def build_context(l: Listing, email: str, *, first_name: str | None = None, is_first: bool = True,
                  today: dt.date | None = None, market=None, unsubscribe_url: str = "") -> dict:
    today = today or dt.date.today()
    f = compute_facts(l)
    copy = write_copy(l, f)
    moves = rank_moves(l, f, market)
    for m in moves:  # don't promise paste text we couldn't produce
        if (m.key == "title" and not copy["title"]) or (m.key == "opener" and not copy["opener"]):
            m.why = m.why.replace(" Exact text in Ready to paste.", "")

    glance = [{"value": f"{l.rating:.2f}", "label": f"Rating · {l.review_count} reviews"}]
    if f.tier and f.to_tier:
        glance.append({"value": str(f.to_tier), "label": f"Five-star reviews to reach {f.tier:g}"})
    if f.top_themes:
        glance.append({"value": f"{f.lead_share}%", "label": f"Of guest mentions are {theme_phrase(f)}"})
    if f.weak:
        glance.append({"value": " & ".join(k for k, _ in f.weak[:2]),
                       "label": f"The score{'s' if len(f.weak) > 1 else ''} below your {l.rating:.2f} overall"})

    math = []
    if f.tier and f.to_tier:
        math.append(f"<b>{f.to_tier}</b> consecutive five-star reviews lift you from {l.rating:.2f} to {f.tier:g}.")
    math.append(f"One 4-star review drops you to {f.after_four_star:.3f} and takes <b>{f.recover}</b> five-stars to win back.")

    notes = [{"kind": "Guests love", "text": f"{t}: {c} mentions."} for t, c in f.top_themes[:2]]
    theme_counts = dict(l.themes)
    for name, val in f.weak[:1]:
        c = theme_counts.get(name)
        notes.append({"kind": "Fix", "text": (f"{name} comes up {c} times and scores {val:.2f}." if c
                                             else f"{name} scores {val:.2f}, your lowest.")})

    paste = []
    if copy["title"]:
        paste.append({"tag": "Listing", "title": "New title",
                      "why": f"Leads with what guests praise most. {len(copy['title'])} of {AIRBNB_TITLE_LIMIT} characters.",
                      "text": copy["title"]})
    if copy["opener"]:
        paste.append({"tag": "Listing", "title": "Description, first line",
                      "why": "The first line shows in search. Lead with what guests love.", "text": copy["opener"]})

    cant = ["Nightly prices for specific dates, yours or your neighbors'. Date-by-date pricing is coming in a future update.",
            "Individual review text. Airbnb doesn't allow it to be collected, so this brief uses Airbnb's own theme counts.",
            "Your bookings, revenue and calendar. These live in your host dashboard."]
    if market is None:
        cant.insert(0, "Market data for your area wasn't available this month, so the market section is left out rather than guessed.")
    if l.missing:
        cant.append(f"Couldn't read from your listing page: {', '.join(l.missing)}.")

    sources = [{"name": "Your Airbnb listing", "url": l.url}]
    if market is not None:
        sources.append({"name": "AirROI (estimates)", "url": "https://www.airroi.com"})

    short = " · ".join(x for x in [f"{l.bedrooms} BR" if l.bedrooms is not None else "",
                                   f"sleeps {l.guests}" if l.guests else ""] if x)
    return dict(
        is_first_brief=is_first, quiet_month=False, market=market, comps=False, checks=None, progress=None,
        sample_banner=None, period_label=today.strftime("%B %Y"),
        preheader=(moves[0].action if moves else "Your listing brief"),
        property={"name": property_name(l.title), "location": l.location, "listing_short": short},
        customer={"first_name": first_name, "email": email}, unsubscribe_url=unsubscribe_url,
        headline=copy["headline"], glance=glance[:4],
        moves=[{"action": m.action, "why": m.why} for m in moves],
        scorecard={"lede": copy["scorecard_lede"], "overall": l.rating,
                   "subscores": [{"name": k, "score": v, "change": None} for k, v in l.subscores.items()],
                   "footnote": f"Overall {l.rating:.2f} · {l.review_count} reviews"
                               + (" · baseline month, so no changes yet" if is_first else ""),
                   "math": math},
        feedback={"lede": copy["feedback_lede"],
                  "themes": [{"name": t, "count": c} for t, c in l.themes[:8]],
                  "footnote": "Mentions counted by Airbnb across your reviews.", "notes": notes},
        paste=paste, cant_check=cant, sources=sources,
        _move_keys=[m.key for m in moves],
    )


def render(context: dict) -> str:
    return _env.get_template("listing_brief.html.j2").render(**context)


def build_brief(listing_url: str, email: str, *, first_name=None, is_first=True, today=None,
                unsubscribe_url="", market_cache=None, session=None) -> tuple[str, dict]:
    """Return (html, context). Raises ListingError if the listing can't be read."""
    today = today or dt.date.today()
    l = read_listing(listing_url, session)
    market = build_market(l, today, market_cache)
    ctx = build_context(l, email, first_name=first_name, is_first=is_first, today=today,
                        market=market, unsubscribe_url=unsubscribe_url)
    return render(ctx), ctx
