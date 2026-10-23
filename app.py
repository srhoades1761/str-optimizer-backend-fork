"""
STR Optimizer API.

Three jobs, nothing else:
  * /api/create-subscription-session  start Stripe Checkout (monthly or annual)
  * /api/stripe/webhook               Stripe tells us who paid; queues their first brief
  * /api/unsubscribe                  one-click email opt-out

The brief itself is built and emailed by worker.py, never inside a request.

This replaces the earlier app (one-time PDF purchase, Firebase login, admin
panel). Its source is in git history; nothing here depends on Firebase.
"""

import logging
import os

from flask import Flask, jsonify
from flask_cors import CORS

from brief.web import bp as brief_bp
from routes_billing import billing_bp
from services import db

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")

app = Flask(__name__)

# The website calls the API through its own domain (Netlify forwards /api/*),
# so browsers normally never make a cross-site request. These origins cover
# direct calls, e.g. while testing on the Netlify preview address.
CORS(app, origins=[o.strip() for o in os.getenv(
    "CORS_ORIGINS", "https://optimizemystr.com,https://www.optimizemystr.com").split(",") if o.strip()])

app.register_blueprint(billing_bp)
app.register_blueprint(brief_bp)

if os.getenv("DATABASE_URL"):
    db.init_pool()
    db.ensure_schema()


@app.route("/")
def index():
    return jsonify({"service": "STR Optimizer API", "status": "running"})


@app.route("/health")
def health():
    ok = True
    try:
        with db.get_cursor() as cur:
            cur.execute("SELECT 1")
    except Exception:
        ok = False
    return jsonify({
        "status": "healthy" if ok else "degraded",
        "database": ok,
        "stripe": bool(os.getenv("STRIPE_SECRET_KEY")),
        "webhook_secret": bool(os.getenv("STRIPE_WEBHOOK_SECRET")),
        "sendgrid": bool(os.getenv("SENDGRID_API_KEY")),
        "market_data": bool(os.getenv("AIRROI_API_KEY")),
        "ai_copy": bool(os.getenv("OPENAI_API_KEY") and os.getenv("BRIEF_LLM_MODEL")),
    }), (200 if ok else 503)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5001)), debug=False)
