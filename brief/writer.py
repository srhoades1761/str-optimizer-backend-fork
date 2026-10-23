"""Prose for the brief. The LLM writes words around computed facts; it never supplies numbers.

One structured call per brief. Every output is validated:
- the title must fit Airbnb's 50-character limit and mention a theme the current title is missing
- any number in the prose must already appear in the computed facts
If the model is unset, fails, or a field fails validation, that field falls back to plain
deterministic wording (or, for paste-ready text, is left out). A brief always sends.
"""
from __future__ import annotations

import json
import os
import re

from .analysis import AIRBNB_TITLE_LIMIT, Facts, mentions, theme_phrase
from .listing import Listing

MODEL_ENV = "BRIEF_LLM_MODEL"  # set to a current OpenAI model id; unset = deterministic prose only
OPENER_LIMIT = 180


def _numbers(text: str) -> set[str]:
    return {n.rstrip(".").replace(",", "") for n in re.findall(r"\d[\d,]*\.?\d*", text or "")}


def allowed_numbers(l: Listing, f: Facts) -> set[str]:
    vals = [l.rating, l.review_count, f.tier, f.to_tier, f.recover, f.theme_total, f.lead_count, f.lead_share,
            l.guests, l.bedrooms, l.beds, l.baths, *[c for _, c in l.themes],
            *l.subscores.values(), *l.subscores_precise.values()]
    out = set()
    for v in vals:
        if v is None:
            continue
        out |= {str(v), f"{v:g}" if isinstance(v, float) else str(v)}
        if isinstance(v, float):
            out |= {f"{v:.1f}", f"{v:.2f}"}
    return out | _numbers(l.title) | _numbers(l.description) | {"1", "2", "3", "4", "5"}


def join_words(items: list[str]) -> str:
    items = list(items)
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1] if items else ""


def fallback(l: Listing, f: Facts) -> dict:
    perfect = [k for k, v in l.subscores.items() if v >= 5.0]
    weak = [k for k, _ in f.weak]
    if weak:
        target = f"{f.tier:g}" if f.tier else "5"
        names = [perfect[0]] + [x.lower() for x in perfect[1:]] if perfect else []
        first = f"{join_words(names)} {'are' if len(perfect) > 1 else 'is'} perfect. " if perfect else ""
        score_lede = (f"{first}{join_words([weak[0]] + [w.lower() for w in weak[1:2]])} "
                      f"{'are' if len(weak) > 1 else 'is'} holding you below {target}.")
    else:
        score_lede = "Every subscore is at or above your overall rating."
    lead_missing = [t for t, _ in f.top_themes[:2] if t in f.title_missing]
    if lead_missing and f.lead_share >= 20:
        headline = (f"Guests already sell your {f.top_themes[0][0].lower()} for you: {f.lead_share}% of everything "
                    f"they mention is {theme_phrase(f)}. Your title doesn't mention {lead_missing[0].lower()} yet.")
    elif f.weak:
        headline = f"Your {f.weak[0][0].lower()} score is the one thing holding your rating back."
    else:
        headline = "Your listing is in good shape. This month is about protecting it."
    top = [t for t, _ in f.top_themes[:2]]
    feedback_lede = (f"{top[0]} and {top[1].lower()} dominate what guests mention." if len(top) == 2
                     else f"{top[0]} leads what guests mention." if top else "Not enough reviews to show themes yet.")
    return {"headline": headline, "scorecard_lede": score_lede, "feedback_lede": feedback_lede,
            "title": None, "opener": None}


PROMPT = """You write short, plain copy for a short-term rental host's monthly listing brief.
Use ONLY the facts given. Do not add any number that is not in the facts. No hype words
(amazing, stunning, hidden gem, oasis). Write like a sharp friend who hosts too.

Return JSON with keys:
- "headline": one or two sentences, the single most useful thing this host should know. Max 220 chars.
- "scorecard_lede": one sentence on which subscores are perfect and which hold the rating back.
- "feedback_lede": one sentence on what guests talk about most.
- "title": a new Airbnb listing title, max {limit} characters INCLUDING spaces, that leads with
  what guests praise most ({missing}). Keep the property's real features. Use " · " as a separator.
- "opener": a new first sentence for the description, max {opener} characters, leading with the same theme.

FACTS:
{facts}
"""


def _facts_payload(l: Listing, f: Facts) -> dict:
    return {
        "current_title": l.title, "location": l.location, "bedrooms": l.bedrooms, "guests": l.guests,
        "rating": l.rating, "reviews": l.review_count, "subscores": l.subscores,
        "weak_subscores": [k for k, _ in f.weak],
        "guest_themes_counts": dict(f.top_themes), "themes_missing_from_title": f.title_missing,
        "lead_theme_share_percent": f.lead_share, "current_first_sentence": f.opener,
        "key_amenities": [a for a in l.amenities if re.search(r"hot tub|pool|view|fireplace|sauna|lake|beach|grill", a, re.I)][:6],
        "description_excerpt": l.description[:1500],
    }


def _call_llm(prompt: str) -> dict | None:
    model = os.environ.get(MODEL_ENV)
    if not model or not os.environ.get("OPENAI_API_KEY"):
        return None
    from openai import OpenAI
    client = OpenAI(timeout=40)
    resp = client.chat.completions.create(
        model=model, max_tokens=700, response_format={"type": "json_object"},
        messages=[{"role": "user", "content": prompt}],
    )
    return json.loads(resp.choices[0].message.content)


def validate(out: dict, l: Listing, f: Facts) -> dict:
    """Keep only fields that pass. Returns a dict with the same keys; failures become None."""
    ok = {}
    allowed = allowed_numbers(l, f)

    def clean_prose(key, limit):
        v = (out or {}).get(key)
        if not isinstance(v, str) or not v.strip() or len(v) > limit:
            return None
        return v.strip() if _numbers(v) <= allowed else None

    ok["headline"] = clean_prose("headline", 240)
    ok["scorecard_lede"] = clean_prose("scorecard_lede", 220)
    ok["feedback_lede"] = clean_prose("feedback_lede", 200)
    t = (out or {}).get("title")
    ok["title"] = (t.strip() if isinstance(t, str) and 10 <= len(t.strip()) <= AIRBNB_TITLE_LIMIT
                   and t.strip() != l.title and _numbers(t) <= allowed
                   and (not f.title_missing or any(mentions(t, m) for m in f.title_missing[:2])) else None)
    o = (out or {}).get("opener")
    ok["opener"] = (o.strip() if isinstance(o, str) and 20 <= len(o.strip()) <= OPENER_LIMIT
                    and _numbers(o) <= allowed else None)
    return ok


def write_copy(l: Listing, f: Facts) -> dict:
    base = fallback(l, f)
    try:
        raw = _call_llm(PROMPT.format(limit=AIRBNB_TITLE_LIMIT, opener=OPENER_LIMIT,
                                      missing=", ".join(f.title_missing[:2]) or "the top guest themes",
                                      facts=json.dumps(_facts_payload(l, f), indent=1)))
    except Exception:  # network, quota, bad JSON: the brief still sends
        raw = None
    if not raw:
        return base
    good = validate(raw, l, f)
    return {k: good.get(k) or base.get(k) for k in base}
