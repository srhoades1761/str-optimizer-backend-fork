"""Billing + brief against a real Postgres.

Needs TEST_DATABASE_URL, e.g. postgresql://postgres@/strtest?host=/tmp&port=5433
(skipped otherwise). Never point this at production: it truncates tables.
"""
import json
import os
import subprocess
import sys
import time
import hmac
import hashlib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PG = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not PG, reason="TEST_DATABASE_URL not set")


@pytest.mark.parametrize("script", ["check_billing.py", "check_routes.py"])
def test_august_billing_suites(script):
    env = {**os.environ, "DATABASE_URL": PG}
    r = subprocess.run([sys.executable, str(ROOT / "tests" / script)], env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-2000:]
    assert " 0 failed" in r.stdout


@pytest.fixture
def pg(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", PG)
    monkeypatch.setenv("PGSSLMODE", "disable")
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_dummy")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_e2e")
    monkeypatch.setenv("STRIPE_PRICE_MONTHLY", "price_M")
    monkeypatch.setenv("STRIPE_PRICE_ANNUAL", "price_A")
    sys.path.insert(0, str(ROOT))
    import importlib
    from services import db, billing
    importlib.reload(billing)  # pick up env above
    db.init_pool()
    db.ensure_schema()
    db.ensure_schema()  # idempotent
    with db.get_cursor(commit=True) as cur:
        cur.execute("TRUNCATE report_jobs, subscribers, processed_events, unsubscribes, market_cache RESTART IDENTITY CASCADE")
    return db, billing


def _signed(app_client, event, secret="whsec_e2e"):
    raw = json.dumps(event).encode()
    ts = int(time.time())
    sig = hmac.new(secret.encode(), f"{ts}.".encode() + raw, hashlib.sha256).hexdigest()
    return app_client.post("/api/stripe/webhook", data=raw,
                           headers={"Stripe-Signature": f"t={ts},v1={sig}", "Content-Type": "application/json"})


EVENT = {"id": "evt_e2e_1", "type": "checkout.session.completed", "data": {"object": {
    "id": "cs_e2e", "mode": "subscription", "payment_status": "paid", "customer": "cus_e2e",
    "subscription": "sub_e2e",
    "metadata": {"plan": "monthly_499", "email": "Host@Example.com",
                 "listing_url": "https://www.airbnb.com/rooms/50439899"}}}}


def test_paid_checkout_to_sent_brief(pg, listing, monkeypatch):
    db, billing = pg
    from app import app
    import worker
    c = app.test_client()

    assert _signed(c, EVENT).status_code == 200
    assert _signed(c, EVENT).get_json()["status"] == "duplicate"   # Stripe retry: no second brief

    sent = []

    def fake_build_and_send(job, sub):
        from brief.pipeline import build_context, render
        ctx = build_context(listing, sub["email"], is_first=job["kind"] == "initial")
        html = render(ctx)
        sent.append((sub["email"], job["kind"], html))
        return {"message_id": "m1", "moves": ctx["_move_keys"], "market": False}

    assert worker.run_once(fake_build_and_send) is True
    assert worker.run_once(fake_build_and_send) is False            # queue empty
    assert len(sent) == 1 and sent[0][1] == "initial"
    assert "Your first listing brief" in sent[0][2]
    with db.get_cursor() as cur:
        cur.execute("SELECT status, summary FROM report_jobs")
        row = cur.fetchone()
    assert row["status"] == "done" and row["summary"]["moves"][0] == "title"


def test_unsubscribed_and_cancelled_are_skipped(pg):
    db, billing = pg
    import worker
    billing.handle_event(EVENT)
    db.unsubscribe("HOST@example.com")
    calls = []
    assert worker.run_once(lambda j, s: calls.append(1)) is True
    assert calls == []
    with db.get_cursor() as cur:
        cur.execute("SELECT status, summary FROM report_jobs")
        assert cur.fetchone()["summary"] == {"skipped": "unsubscribed"}


def test_bad_listing_fails_once_not_three_times(pg):
    db, billing = pg
    import worker
    from brief.listing import ListingError
    billing.handle_event({**EVENT, "id": "evt_e2e_2"})

    def boom(j, s):
        raise ListingError("private listing")
    worker.run_once(boom)
    assert worker.run_once(boom) is False
    with db.get_cursor() as cur:
        cur.execute("SELECT status, attempts, last_error FROM report_jobs")
        r = cur.fetchone()
    assert r["status"] == "failed" and r["attempts"] == 1 and "private" in r["last_error"]


def test_transient_error_retries(pg):
    db, billing = pg
    import worker
    billing.handle_event({**EVENT, "id": "evt_e2e_3"})

    def flaky(j, s):
        raise TimeoutError("sendgrid slow")
    worker.run_once(flaky)
    with db.get_cursor() as cur:
        cur.execute("SELECT status FROM report_jobs")
        assert cur.fetchone()["status"] == "pending"


def test_market_cache_roundtrip(pg):
    db, _ = pg
    from brief.cache import DbMarketCache
    from brief.market import MarketStats, Month
    c = DbMarketCache(db)
    s = MarketStats("Pagosa Springs", [Month(m, 0.3, 250.0, 2000.0) for m in range(1, 13)], 854, "TTM")
    key = ("Pagosa Springs, CO", 2, "2026-10")
    assert key not in c
    c[key] = s
    assert c[key] == s


def test_unsubscribe_routes(pg):
    db, _ = pg
    from app import app
    from brief.send import unsubscribe_token
    c = app.test_client()
    t = unsubscribe_token("host@example.com")
    assert c.get(f"/api/unsubscribe?t={t}").status_code == 200
    assert not db.is_unsubscribed("host@example.com")                 # GET never unsubscribes
    r = c.post(f"/api/unsubscribe?t={t}", data={"List-Unsubscribe": "One-Click"})
    assert r.status_code == 200 and db.is_unsubscribed("host@example.com")
    assert c.get("/api/unsubscribe?t=forged").status_code == 400


def test_health(pg):
    from app import app
    r = app.test_client().get("/health")
    assert r.status_code == 200 and r.get_json()["database"] is True
