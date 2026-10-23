import os, sys, json, time, hmac, hashlib
os.environ.setdefault("DATABASE_URL","postgresql://postgres@/strtest?host=/tmp&port=5433")
os.environ["PGSSLMODE"]="disable"; os.environ["STRIPE_SECRET_KEY"]="sk_test_dummy"
os.environ["STRIPE_PRICE_MONTHLY"]="price_M"; os.environ["STRIPE_PRICE_ANNUAL"]="price_A"
os.environ["STRIPE_WEBHOOK_SECRET"]="whsec_testsecret123"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from flask import Flask
from routes_billing import billing_bp
from services import db
db.init_pool(); db.ensure_schema()

app = Flask(__name__); app.register_blueprint(billing_bp)
c = app.test_client()
P,F=[],[]
def chk(n,cond,d=""):
    (P if cond else F).append(n); print(("  PASS  " if cond else "  FAIL  ")+n+(f"  {d}" if d and not cond else ""))

with db.get_cursor(commit=True) as cur:
    cur.execute("TRUNCATE report_jobs, subscribers, processed_events RESTART IDENTITY CASCADE")

print("\n--- checkout endpoint validation ---")
r = c.post("/api/create-subscription-session", json={})
chk("empty body -> 400", r.status_code==400)
chk("error is user-safe", "error" in r.get_json() and "Traceback" not in r.get_data(as_text=True))

r = c.post("/api/create-subscription-session", json={"plan":"monthly_499","email":"bad","listingUrl":"https://www.airbnb.com/rooms/1"})
chk("bad email -> 400", r.status_code==400, r.get_data(as_text=True)[:80])

r = c.post("/api/create-subscription-session", json={"plan":"monthly_499","email":"a@b.com","listingUrl":"http://169.254.169.254/"})
chk("SSRF url -> 400", r.status_code==400)

r = c.post("/api/create-subscription-session", json={"plan":"hacked","email":"a@b.com","listingUrl":"https://www.airbnb.com/rooms/1"})
chk("unknown plan -> 400", r.status_code==400)

print("\n--- webhook endpoint ---")
def sign(b,s,ts=None):
    ts=ts or int(time.time())
    return f"t={ts},v1="+hmac.new(s.encode(), f"{ts}.".encode()+b, hashlib.sha256).hexdigest()

evt={"id":"evt_r1","type":"checkout.session.completed","data":{"object":{
  "id":"cs_r1","payment_status":"paid","customer":"cus_r1","subscription":"sub_r1",
  "metadata":{"plan":"monthly_499","email":"r@example.com","listing_url":"https://www.airbnb.com/rooms/9"}}}}
raw=json.dumps(evt).encode()

r=c.post("/api/stripe/webhook", data=raw, headers={"Stripe-Signature":"garbage","Content-Type":"application/json"})
chk("bad signature -> 400", r.status_code==400)

r=c.post("/api/stripe/webhook", data=raw, headers={"Content-Type":"application/json"})
chk("missing signature -> 400", r.status_code==400)

r=c.post("/api/stripe/webhook", data=raw, headers={"Stripe-Signature":sign(raw,"whsec_testsecret123"),"Content-Type":"application/json"})
chk("valid webhook -> 200", r.status_code==200, r.get_data(as_text=True))
chk("subscriber provisioned", db.get_by_subscription_id("sub_r1") is not None)

r2=c.post("/api/stripe/webhook", data=raw, headers={"Stripe-Signature":sign(raw,"whsec_testsecret123"),"Content-Type":"application/json"})
chk("retry -> 200 duplicate", r2.status_code==200 and r2.get_json().get("status")=="duplicate")

with db.get_cursor() as cur:
    cur.execute("SELECT COUNT(*) n FROM report_jobs"); chk("still exactly one job", cur.fetchone()["n"]==1)

print("\n--- status endpoint ---")
r=c.post("/api/subscription-status", json={"email":"r@example.com"})
j=r.get_json(); chk("status active", r.status_code==200 and j["active"] is True)
chk("listing returned", j["listings"] and j["listings"][0]["plan"]=="monthly_499")
r=c.post("/api/subscription-status", json={"email":"nobody@example.com"})
chk("unknown email inactive", r.get_json()["active"] is False)

print(f"\n{'='*46}\n  {len(P)} passed, {len(F)} failed")
if F: print("  FAILED: "+", ".join(F))
sys.exit(1 if F else 0)
