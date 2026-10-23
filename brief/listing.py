"""Read a public Airbnb listing page into a Listing.

Airbnb embeds the page data as JSON in <script id="data-deferred-state-0">.
Paths below were verified against a live listing page on 2026-10-15
(tests/fixtures/hermosa_pdp.json is a trimmed copy of that page's data).
Every field is looked up by its known path first, then by a key search,
so a moved field degrades to "missing" instead of crashing the brief.

Only the public listing page is read. /rooms/<id>/reviews is disallowed by
robots.txt and is never fetched; review themes come from Airbnb's own
theme counts on the listing page.
"""
from __future__ import annotations

import html
import ipaddress
import json
import re
import socket
from dataclasses import dataclass, field
from urllib.parse import urlparse

import requests

ALLOWED_HOSTS = {"airbnb.com", "www.airbnb.com"}
LISTING_HOST_SUFFIXES = ("airbnb.com", "airbnb.co.uk", "airbnb.ca", "airbnb.com.au")
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
SUBSCORE_ORDER = ["Cleanliness", "Accuracy", "Check-in", "Communication", "Location", "Value"]
_PRECISE_KEYS = {  # unrounded subscores from the page's logging block
    "Cleanliness": "cleanlinessRating", "Accuracy": "accuracyRating", "Check-in": "checkinRating",
    "Communication": "communicationRating", "Location": "locationRating", "Value": "valueRating",
}


class ListingError(Exception):
    """The URL is not a supported listing, or the page could not be read."""


@dataclass
class Listing:
    listing_id: str
    url: str
    title: str
    location: str
    lat: float | None
    lng: float | None
    rating: float | None
    review_count: int
    subscores: dict[str, float]            # display values, e.g. {"Cleanliness": 4.7}
    subscores_precise: dict[str, float]    # unrounded, e.g. {"Cleanliness": 4.73}
    themes: list[tuple[str, int]]          # [("Location", 86), ...] sorted by count desc
    description: str                       # plain text, sections joined
    description_sections: dict[str, str]
    amenities: list[str]
    bedrooms: int | None
    beds: int | None
    baths: float | None
    guests: int | None
    guest_favorite: bool = False
    missing: list[str] = field(default_factory=list)  # fields we could not read

    @property
    def title_length(self) -> int:
        return len(self.title)


# ---------------------------------------------------------------- fetching

def normalize_url(url: str) -> tuple[str, str]:
    """Return (canonical_url, listing_id). Rejects anything but an Airbnb room URL."""
    p = urlparse(url.strip())
    host = (p.hostname or "").lower()
    if p.scheme not in ("http", "https") or not any(host == h or host.endswith("." + h) for h in LISTING_HOST_SUFFIXES):
        raise ListingError("Only Airbnb listing URLs are supported for now.")
    m = re.match(r"^/rooms/(?:plus/)?(\d+)", p.path)
    if not m:
        raise ListingError("That doesn't look like an Airbnb listing URL (/rooms/<number>).")
    lid = m.group(1)
    return f"https://www.airbnb.com/rooms/{lid}", lid


def _assert_public_host(host: str) -> None:
    """SSRF guard: refuse hosts that resolve to private or link-local addresses."""
    for info in socket.getaddrinfo(host, 443):
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            raise ListingError("Refusing to fetch a non-public address.")


def fetch_listing_html(url: str, session: requests.Session | None = None) -> str:
    canonical, _ = normalize_url(url)
    _assert_public_host("www.airbnb.com")
    s = session or requests.Session()
    r = s.get(canonical, headers={"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"},
              timeout=20, allow_redirects=True)
    if urlparse(r.url).hostname not in ALLOWED_HOSTS:
        raise ListingError("Listing page redirected off airbnb.com.")
    if r.status_code == 404:
        raise ListingError("Airbnb says this listing doesn't exist or isn't public.")
    r.raise_for_status()
    return r.text


def extract_page_data(page_html: str) -> dict:
    m = re.search(r'<script[^>]+id="data-deferred-state-0"[^>]*>(.*?)</script>', page_html, re.S)
    if not m:
        raise ListingError("Couldn't find listing data on the page (layout may have changed).")
    return json.loads(m.group(1))


# ---------------------------------------------------------------- parsing helpers

def _get(d, *path):
    for p in path:
        if isinstance(d, dict):
            d = d.get(p)
        elif isinstance(d, list) and isinstance(p, int) and -len(d) <= p < len(d):
            d = d[p]
        else:
            return None
    return d


def _find_key(o, key, depth=0):
    """First value for `key` anywhere in the tree (fallback when a path moves)."""
    if depth > 50:
        return None
    if isinstance(o, dict):
        if o.get(key) is not None:
            return o[key]
        children = o.values()
    elif isinstance(o, list):
        children = o
    else:
        return None
    for v in children:
        hit = _find_key(v, key, depth + 1)
        if hit is not None:
            return hit
    return None


def _strip_html(s: str) -> str:
    s = re.sub(r"<br\s*/?>", "\n", s or "")
    s = re.sub(r"<[^>]+>", "", s)
    return re.sub(r"\n{3,}", "\n\n", html.unescape(s)).strip()


def _float(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def parse_listing(data: dict, url: str) -> Listing:
    canonical, lid = normalize_url(url)
    root = _get(data, "niobeClientData", 0, 1, "data") or data
    pdp = _get(root, "node", "pdpPresentation") or {}
    meta = _get(root, "presentation", "stayProductDetailPage", "sections", "metadata") or {}
    log = _get(meta, "loggingContext", "eventDataLogging") or _find_key(root, "eventDataLogging") or {}
    share = _get(meta, "sharingConfig") or {}
    quality = _get(pdp, "quality") or {}
    missing: list[str] = []

    title = (_get(pdp, "title", "content", "localizedString")
             or _get(pdp, "title", "content", "localizedStringWithTranslationPreference") or "")
    if not title:
        missing.append("title")

    rating = _float(share.get("starRating")) or _float(log.get("guestSatisfactionOverall"))
    count = share.get("reviewCount") or log.get("visibleReviewCount") or 0
    try:
        count = int(count)
    except (TypeError, ValueError):
        count = 0
    if rating is None:
        missing.append("rating")

    cats = quality.get("categoryRatings") or _find_key(root, "categoryRatings") or []
    subscores = {}
    for c in cats:
        label, val = c.get("label"), _float(c.get("localizedRating"))
        if label in SUBSCORE_ORDER and val is not None:
            subscores[label] = val
    subscores = {k: subscores[k] for k in SUBSCORE_ORDER if k in subscores}
    precise = {k: round(_float(log.get(v)), 2) for k, v in _PRECISE_KEYS.items() if _float(log.get(v)) is not None}
    if not subscores:
        missing.append("subscores")

    tags = quality.get("reviewTags") or _find_key(root, "reviewTags") or []
    themes = sorted(((t.get("localizedName") or t.get("name", "").title(), int(t.get("count") or 0))
                     for t in tags if t.get("count")), key=lambda x: (-x[1], x[0]))
    if not themes:
        missing.append("review themes")

    loc = pdp.get("location") or {}
    lat = _float(loc.get("latitude")) or _float(log.get("listingLat"))
    lng = _float(loc.get("longitude")) or _float(log.get("listingLng"))
    location = loc.get("subtitle") or ""
    if location.endswith(", United States"):
        location = location[: -len(", United States")]

    sections: dict[str, str] = {}
    secs = _get(root, "presentation", "stayProductDetailPage", "sections", "sections") or []
    modal = next((s.get("section") for s in secs if s.get("sectionId") == "DESCRIPTION_MODAL"), None) or {}
    for item in modal.get("items") or []:
        text = _strip_html(_get(item, "html", "htmlText") or "")
        if text:
            sections[item.get("title") or "Summary"] = text
    if not sections:
        missing.append("description")

    amenities = [a["title"] for g in _get(pdp, "amenities", "seeAllAmenitiesGroups") or []
                 for a in g.get("amenities") or [] if a.get("available") and a.get("title")]

    # "Home in Pagosa Springs · ★4.87 · 2 bedrooms · 4 beds · 1.5 baths"
    summary = share.get("title") or ""
    def _num(pattern):
        m = re.search(pattern, summary)
        return float(m.group(1)) if m else None
    bedrooms = _num(r"(\d+)\s+bedrooms?")
    if bedrooms is None and re.search(r"\bStudio\b", summary, re.I):
        bedrooms = 0
    beds, baths = _num(r"(\d+)\s+beds?\b"), _num(r"([\d.]+)\s+(?:shared\s+|private\s+)?baths?")
    guests = share.get("personCapacity") or log.get("personCapacity")

    return Listing(
        listing_id=lid, url=canonical, title=title, location=location, lat=lat, lng=lng,
        rating=rating, review_count=count, subscores=subscores, subscores_precise=precise,
        themes=themes, description="\n\n".join(sections.values()), description_sections=sections,
        amenities=amenities, bedrooms=int(bedrooms) if bedrooms is not None else None,
        beds=int(beds) if beds is not None else None, baths=baths,
        guests=int(guests) if guests else None,
        guest_favorite=bool(quality.get("isGuestFavorite")), missing=missing,
    )


def read_listing(url: str, session: requests.Session | None = None) -> Listing:
    return parse_listing(extract_page_data(fetch_listing_html(url, session)), url)
