"""Market analysis from AirROI (https://www.airroi.com/api).

Cost per brief: lookup $0.01 + all-metrics (market) $0.50 + all-metrics (bedroom cohort) $0.50.
Results are cached per (market, bedrooms, month) for up to 30 days, the limit in AirROI's terms,
so subscribers in the same market share one pull.

AirROI display terms (small business): figures may be shown to customers as aggregated estimates,
credited to AirROI. Raw listing-level data is never shown. The brief's source line does this.

VERIFY ON FIRST LIVE CALL: AirROI's public docs do not show the response schema for
/markets/metrics/all. `_monthly_rows` accepts the likely shapes and raises MarketDataError
on anything else, so a schema mismatch withholds the section instead of printing wrong numbers.
Run `python -m brief.market "Pagosa Springs" 37.26649 -107.00503 2` with AIRROI_API_KEY set and
check the printed rows before launch.
"""
from __future__ import annotations

import datetime as dt
import os
from dataclasses import dataclass, field

import requests

API = "https://api.airroi.com"
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
MONTH_NAMES = ["January", "February", "March", "April", "May", "June", "July", "August",
               "September", "October", "November", "December"]
MIN_COHORT_LISTINGS = 15  # below this, a bedroom cohort is too small to show


class MarketDataError(Exception):
    pass


@dataclass
class Month:
    month: int          # 1-12
    occupancy: float    # 0-1
    adr: float
    revenue: float | None


@dataclass
class MarketStats:
    name: str
    months: list[Month]          # trailing 12 months, any order
    active_listings: int | None
    as_of: str                   # e.g. "Oct 2025–Sep 2026"


@dataclass
class MarketFacts:
    area: str
    season: list[dict]
    now_label: str
    lede: str
    columns: list[str]
    rows: list[dict]
    findings: list[str]
    coming_up: list[dict]
    so_what: str
    source_note: str
    action: str = ""
    action_why: str = ""
    extra: dict = field(default_factory=dict)


# ---------------------------------------------------------------- API client

class AirROI:
    def __init__(self, api_key: str | None = None, session: requests.Session | None = None):
        self.key = api_key or os.environ.get("AIRROI_API_KEY")
        if not self.key:
            raise MarketDataError("AIRROI_API_KEY is not set")
        self.s = session or requests.Session()

    def _req(self, method, path, **kw):
        r = self.s.request(method, API + path, headers={"x-api-key": self.key}, timeout=30, **kw)
        if r.status_code >= 400:
            raise MarketDataError(f"AirROI {path} returned {r.status_code}")
        return r.json()

    def lookup(self, lat: float, lng: float) -> dict:
        m = self._req("GET", "/markets/lookup", params={"lat": lat, "lng": lng})
        market = {k: m.get(k) for k in ("country", "region", "locality", "district") if m.get(k)}
        if not all(k in market for k in ("country", "region", "locality")):
            raise MarketDataError("No market found for this location")
        market["full_name"] = m.get("full_name") or market["locality"]
        return market

    def all_metrics(self, market: dict, bedrooms: int | None = None) -> dict:
        body = {"market": {k: v for k, v in market.items() if k != "full_name"},
                "currency": "usd", "num_months": 12}
        if bedrooms is not None:
            body["filter"] = {"bedrooms": {"eq": bedrooms}}
        return self._req("POST", "/markets/metrics/all", json=body)


def _num(d: dict, *keys):
    for k in keys:
        v = d.get(k)
        if isinstance(v, dict):  # e.g. {"avg": 0.32} or {"value": ...}
            v = v.get("avg", v.get("value", v.get("mean")))
        if isinstance(v, (int, float)):
            return float(v)
    return None


def _monthly_rows(resp: dict) -> list[Month]:
    """Normalize AirROI's monthly series. Accepts either a list of month records
    (results/data/monthly) or parallel per-metric series keyed by date."""
    rows = resp.get("results") or resp.get("monthly") or resp.get("data")
    out: list[Month] = []
    if isinstance(rows, list) and rows and isinstance(rows[0], dict):
        for r in rows:
            date = str(r.get("date") or r.get("month") or "")
            try:
                month = int(date[5:7]) if len(date) >= 7 else int(date)
            except ValueError:
                continue
            occ = _num(r, "occupancy", "occupancy_rate")
            adr = _num(r, "average_daily_rate", "adr")
            rev = _num(r, "revenue", "avg_revenue")
            if occ is not None and adr is not None:
                out.append(Month(month, occ / 100 if occ > 1 else occ, adr, rev))
    elif isinstance(resp.get("occupancy"), list):
        by = {}
        for metric, key in (("occupancy", "occupancy"), ("average_daily_rate", "adr"), ("revenue", "revenue")):
            for p in resp.get(metric) or []:
                m = int(str(p.get("date"))[5:7])
                by.setdefault(m, {})[key] = _num(p, "value", "avg")
        for m, v in by.items():
            if v.get("occupancy") is not None and v.get("adr") is not None:
                occ = v["occupancy"]
                out.append(Month(m, occ / 100 if occ > 1 else occ, v["adr"], v.get("revenue")))
    if len(out) < 12:
        raise MarketDataError(f"Expected 12 monthly rows, got {len(out)}; check AirROI response schema")
    return out[-12:]


def fetch_stats(client: AirROI, lat: float, lng: float, bedrooms: int | None, cache=None) -> tuple[MarketStats, MarketStats | None]:
    market = client.lookup(lat, lng)
    month_key = dt.date.today().strftime("%Y-%m")

    def pull(beds):
        key = (market["full_name"], beds, month_key)
        if cache is not None and key in cache:
            return cache[key]
        resp = client.all_metrics(market, beds)
        stats = MarketStats(
            name=market["locality"], months=_monthly_rows(resp),
            active_listings=int(_num(resp, "active_listings_count", "active_listings") or 0) or None,
            as_of=resp.get("period") or "trailing 12 months",
        )
        if cache is not None:
            cache[key] = stats
        return stats

    whole = pull(None)
    cohort = None
    if bedrooms is not None:
        try:
            cohort = pull(bedrooms)
            if cohort.active_listings is not None and cohort.active_listings < MIN_COHORT_LISTINGS:
                cohort = None
        except MarketDataError:
            cohort = None
    return whole, cohort


# ---------------------------------------------------------------- analysis (pure, tested)

def classify_season(months: list[Month]) -> dict[int, str]:
    """Top three occupancy months are peak, bottom three are slow, the rest shoulder."""
    ranked = sorted(months, key=lambda m: (-m.occupancy, m.month))
    levels = {m.month: "shoulder" for m in months}
    for m in ranked[:3]:
        levels[m.month] = "peak"
    for m in ranked[-3:]:
        levels[m.month] = "slow"
    return levels


def upcoming_dates(today: dt.date, days: int = 60) -> list[dict]:
    """US demand dates inside the window. Deterministic, no API."""
    y = today.year

    def nth_weekday(year, month, weekday, n):
        d = dt.date(year, month, 1)
        d += dt.timedelta(days=(weekday - d.weekday()) % 7)
        return d + dt.timedelta(weeks=n - 1)

    def last_weekday(year, month, weekday):
        d = dt.date(year, month + 1, 1) - dt.timedelta(days=1) if month < 12 else dt.date(year, 12, 31)
        return d - dt.timedelta(days=(d.weekday() - weekday) % 7)

    events = []
    for yr in (y, y + 1):
        events += [
            (last_weekday(yr, 5, 0), "Memorial Day weekend."),
            (dt.date(yr, 7, 4), "Fourth of July."),
            (nth_weekday(yr, 9, 0, 1), "Labor Day weekend."),
            (nth_weekday(yr, 11, 3, 4), "Thanksgiving."),
            (dt.date(yr, 12, 24), "Christmas through New Year's."),
            (nth_weekday(yr, 1, 0, 3), "Martin Luther King Jr. weekend."),
            (nth_weekday(yr, 2, 0, 3), "Presidents' Day weekend."),
        ]
    end = today + dt.timedelta(days=days)
    return [{"when": d.strftime("%b %-d"), "what": w} for d, w in sorted(events) if today <= d <= end]


def analyze_market(whole: MarketStats, cohort: MarketStats | None, bedrooms: int | None,
                   today: dt.date, sources: str) -> MarketFacts:
    levels = classify_season(whole.months)
    by = {m.month: m for m in whole.months}
    now = today.month
    nxt = now % 12 + 1
    after = nxt % 12 + 1
    peak = [m for m in whole.months if levels[m.month] == "peak"]
    slow = [m for m in whole.months if levels[m.month] == "slow"]
    peak_occ = sum(m.occupancy for m in peak) / len(peak) * 100
    slow_occ = sum(m.occupancy for m in slow) / len(slow) * 100

    findings = [f"Peak months run about <b>{peak_occ:.1f}%</b> occupancy, slow months "
                f"<b>{slow_occ:.1f}%</b>. That {peak_occ - slow_occ:.1f}-point gap is the year's story."]
    with_rev = [m for m in whole.months if m.revenue]
    if len(with_rev) == 12:
        best = max(with_rev, key=lambda m: m.revenue)
        worst = min(with_rev, key=lambda m: m.revenue)
        ratio = best.revenue / worst.revenue
        adr_move = (best.adr / worst.adr - 1) * 100
        lesson = ("Filling slow-month nights matters more than squeezing peak prices."
                  if adr_move < (ratio - 1) * 100 / 2 else "Rate swings drive the year here, so price by season.")
        findings.append(f"The best month ({MONTH_NAMES[best.month - 1]}) earns <b>{ratio:.2f}×</b> the worst "
                        f"({MONTH_NAMES[worst.month - 1]}), and the nightly rate moves <b>{adr_move:.0f}%</b>. {lesson}")

    def avg(stats, attr):
        return sum(getattr(m, attr) for m in stats.months) / 12

    cols = ["Whole market"] + ([f"{bedrooms}-BR homes"] if cohort else [])
    rows = [
        {"label": "Avg nightly rate", "values": [f"${avg(whole, 'adr'):,.0f}"] + ([f"${avg(cohort, 'adr'):,.0f}"] if cohort else [])},
        {"label": "Occupancy", "values": [f"{avg(whole, 'occupancy') * 100:.1f}%"] + ([f"{avg(cohort, 'occupancy') * 100:.1f}%"] if cohort else [])},
        {"label": "Active listings", "values": [f"{whole.active_listings:,}" if whole.active_listings else "—"]
         + ([f"{cohort.active_listings:,}" if cohort.active_listings else "—"] if cohort else [])},
    ]

    nl, xl = levels[nxt], levels[after]
    nname, aname = MONTH_NAMES[nxt - 1], MONTH_NAMES[after - 1]
    if nl == "slow":
        action = f"Open {nname} with a 2-night minimum and 15% off midweek."
        why = f"{nname} is one of {whole.name}'s three slowest months, at about {by[nxt].occupancy * 100:.0f}% occupancy."
        so_what = f"Treat {nname} as a fill month: shorter minimums and midweek discounts."
    elif nl == "peak":
        action = f"Hold your {nname} rates and add a 3-night minimum on weekends."
        why = f"{nname} is one of {whole.name}'s three busiest months. It books without discounts."
        so_what = f"Treat {nname} as a hold month: don't discount it."
    else:
        action, why = "", ""
        so_what = f"{nname} is a shoulder month here. Price weekends near the market and watch the calendar."
    if xl == "peak" and nl != "peak":
        so_what += f" {aname} is a peak month, so protect it now: no early discounts."
    elif xl == "slow" and nl != "slow":
        so_what += f" {aname} is a slow month; set your fill tactics before it arrives."

    lede_bits = []
    if nl in ("slow", "peak"):
        lede_bits.append(f"{nname} is one of {whole.name}'s three {'slowest' if nl == 'slow' else 'busiest'} months")
    if xl in ("slow", "peak") and xl != nl:
        lede_bits.append(f"{aname} is one of its three {'slowest' if xl == 'slow' else 'busiest'}")
    lede = (" and ".join(lede_bits) + ".") if lede_bits else f"{nname} and {aname} are shoulder months in {whole.name}."

    return MarketFacts(
        area=whole.name,
        season=[{"name": MONTHS[m - 1][0], "level": levels[m], "now": m == now} for m in range(1, 13)],
        now_label=f"▲ You are here ({MONTHS[now - 1]})",
        lede=lede, columns=cols, rows=rows, findings=findings,
        coming_up=upcoming_dates(today, 90), so_what=so_what, source_note=sources,
        action=action, action_why=why,
    )


if __name__ == "__main__":  # live check: python -m brief.market "Name" lat lng bedrooms
    import sys
    c = AirROI()
    w, co = fetch_stats(c, float(sys.argv[2]), float(sys.argv[3]), int(sys.argv[4]) if len(sys.argv) > 4 else None)
    for s in (w, co):
        if s:
            print(s.name, s.active_listings)
            for m in sorted(s.months, key=lambda m: m.month):
                print(f"  {MONTHS[m.month - 1]}  occ {m.occupancy:.3f}  adr {m.adr:.0f}  rev {m.revenue}")
