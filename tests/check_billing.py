"""Exercises the billing layer against a real Postgres and the real Stripe SDK."""

import os, sys, json, time, hmac, hashlib

os.environ.setdefault("DATABASE_URL", "postgresql://postgres@/strtest?host=/tmp&port=5433")
os.environ["PGSSLMODE"] = "disable"
os.environ["STRIPE_SECRET_KEY"] = "sk_test_dummy"
os.environ["STRIPE_PRICE_MONTHLY"] = "price_1U5YijRvCUJT12uKVmU1JpKi"
os.environ["STRIPE_PRICE_ANNUAL"] = "price_1U5YijRvCUJT12uKEgUKLGJz"
os.environ["STRIPE_WEBHOOK_SECRET"] = "whsec_testsecret123"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from services import db, billing  # noqa: E402
db.init_pool(); db.ensure_schema()

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS  " if cond else "  FAIL  ") + name + (f"   {detail}" if detail and not cond else ""))


print("\n--- validation ---")
for bad in ["", "nope", "a@b", "a b@c.com"]:
    try:
        billing.validate_email(bad); check(f"email rejects {bad!r}", False)
    except billing.BillingError:
        check(f"email rejects {bad!r}", True)
check("email accepts good", billing.validate_email(" Scott@Example.com ") == "Scott@Example.com")

print("\n--- SSRF / listing host guard ---")
blocked = [
    "http://169.254.169.254/latest/meta-data/",
    "http://localhost:5000/admin",
    "https://evil.com/airbnb.com",
    "file:///etc/passwd",
    "https://airbnb.com.attacker.net/rooms/1",
    "https://www.vrbo.com/12345",            # brief reads Airbnb only
    "https://www.booking.com/hotel/x.html",
    "https://www.airbnb.com/s/Pagosa/homes",  # a search page, not a listing
    "",
]
for u in blocked:
    try:
        billing.validate_listing_url(u); check(f"blocks {u[:42]!r}", False)
    except billing.BillingError:
        check(f"blocks {u[:42]!r}", True)

for u in ["https://www.airbnb.com/rooms/50439899",
          "https://airbnb.co.uk/rooms/1"]:
    try:
        billing.validate_listing_url(u); check(f"allows {u[:42]!r}", True)
    except billing.BillingError as e:
        check(f"allows {u[:42]!r}", False, str(e))

print("\n--- price mapping (no client-supplied amounts) ---")
check("monthly maps", billing.PRICE_IDS["monthly_499"] == "price_1U5YijRvCUJT12uKVmU1JpKi")
check("annual maps", billing.PRICE_IDS["annual_4788"] == "price_1U5YijRvCUJT12uKEgUKLGJz")
try:
    billing.create_subscription_session("free_lol", "a@b.com", "https://www.airbnb.com/rooms/1")
    check("unknown plan rejected", False)
except billing.BillingError:
    check("unknown plan rejected", True)

print("\n--- webhook signature ---")
def sign(payload_bytes, secret, ts=None):
    ts = ts or int(time.time())
    sig = hmac.new(secret.encode(), f"{ts}.".encode() + payload_bytes, hashlib.sha256).hexdigest()
    return f"t={ts},v1={sig}"

evt = {"id": "evt_test_1", "type": "checkout.session.completed",
       "data": {"object": {"id": "cs_1", "payment_status": "paid",
                           "customer": "cus_1", "subscription": "sub_1",
                           "metadata": {"plan": "monthly_499", "email": "host@example.com",
                                        "listing_url": "https://www.airbnb.com/rooms/50439899"}}}}
raw = json.dumps(evt).encode()

try:
    e = billing.verify_webhook(raw, sign(raw, "whsec_testsecret123"))
    check("valid signature accepted", e["id"] == "evt_test_1")
except Exception as ex:
    check("valid signature accepted", False, repr(ex))

try:
    billing.verify_webhook(raw, sign(raw, "whsec_WRONG"))
    check("bad signature rejected", False)
except Exception:
    check("bad signature rejected", True)

try:
    billing.verify_webhook(raw, sign(raw, "whsec_testsecret123", ts=int(time.time()) - 3600))
    check("replay (old timestamp) rejected", False)
except Exception:
    check("replay (old timestamp) rejected", True)

# The classic production bug: re-serialising the body before verifying.
reserialised = json.dumps(json.loads(raw)).encode() + b" "
try:
    billing.verify_webhook(reserialised, sign(raw, "whsec_testsecret123"))
    check("tampered body rejected", False)
except Exception:
    check("tampered body rejected", True)

print("\n--- database ---")
with db.get_cursor(commit=True) as c:
    c.execute("TRUNCATE report_jobs, subscribers, processed_events RESTART IDENTITY CASCADE")

check("event not yet processed", db.event_already_processed("evt_test_1") is False)
check("same event now duplicate", db.event_already_processed("evt_test_1") is True)

res = billing.handle_event(evt)
row = db.get_by_subscription_id("sub_1")
check("subscriber created", row is not None, res)
check("status active", row and row["status"] == "active")
check("plan recorded", row and row["plan"] == "monthly_499")
check("customer id stored", row and row["stripe_customer_id"] == "cus_1")

with db.get_cursor() as c:
    c.execute("SELECT COUNT(*) n FROM report_jobs WHERE subscriber_id=%s", (row["id"],))
    check("initial report queued", c.fetchone()["n"] == 1)

billing.handle_event(evt)  # simulate a retry that slipped past idempotency
with db.get_cursor() as c:
    c.execute("SELECT COUNT(*) n FROM report_jobs WHERE subscriber_id=%s", (row["id"],))
    check("replay does not double-queue", c.fetchone()["n"] == 1)

print("\n--- lifecycle ---")
billing.handle_event({"id": "evt_2", "type": "customer.subscription.updated",
    "data": {"object": {"id": "sub_1", "status": "active", "cancel_at_period_end": True,
                        "current_period_end": 1800000000,
                        "cancellation_details": {"feedback": "too_expensive"},
                        "items": {"data": [{"price": {"id": "price_1U5YijRvCUJT12uKEgUKLGJz"}}]}}}})
row = db.get_by_subscription_id("sub_1")
check("cancel_at_period_end set", row["cancel_at_period_end"] is True)
check("cancellation reason captured", row["cancellation_reason"] == "too_expensive")
check("plan switched to annual", row["plan"] == "annual_4788")
check("period end stored", row["current_period_end"] is not None)

billing.handle_event({"id": "evt_3", "type": "invoice.payment_failed",
                      "data": {"object": {"id": "in_1", "subscription": "sub_1"}}})
check("past_due on failed invoice", db.get_by_subscription_id("sub_1")["status"] == "past_due")

billing.handle_event({"id": "evt_4", "type": "invoice.paid",
                      "data": {"object": {"id": "in_2", "subscription": "sub_1"}}})
check("active again on paid", db.get_by_subscription_id("sub_1")["status"] == "active")

check("has_active_subscription", db.has_active_subscription("HOST@example.com") is True)

billing.handle_event({"id": "evt_5", "type": "customer.subscription.deleted",
                      "data": {"object": {"id": "sub_1",
                                          "cancellation_details": {"feedback": "missing_features"}}}})
check("canceled on delete", db.get_by_subscription_id("sub_1")["status"] == "canceled")
check("no longer active", db.has_active_subscription("host@example.com") is False)

print("\n--- unpaid checkout must not provision ---")
unpaid = {"id": "evt_6", "type": "checkout.session.completed",
          "data": {"object": {"id": "cs_2", "payment_status": "unpaid",
                              "customer": "cus_2", "subscription": "sub_2",
                              "metadata": {"plan": "monthly_499", "email": "x@y.com",
                                           "listing_url": "https://www.airbnb.com/rooms/2"}}}}
billing.handle_event(unpaid)
check("unpaid session not provisioned", db.get_by_subscription_id("sub_2") is None)

print("\n--- job queue ---")
with db.get_cursor(commit=True) as c:
    c.execute("UPDATE report_jobs SET status='pending'")
job = db.claim_next_job()
check("job claimed", job is not None)
check("claim is exclusive", db.claim_next_job() is None)
db.finish_job(job["id"], pdf_url="/api/download/x.pdf")
with db.get_cursor() as c:
    c.execute("SELECT status, pdf_url FROM report_jobs WHERE id=%s", (job["id"],))
    r = c.fetchone()
    check("job done + url", r["status"] == "done" and r["pdf_url"] == "/api/download/x.pdf")

print(f"\n{'='*46}\n  {len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("  FAILED: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
