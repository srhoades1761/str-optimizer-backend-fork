"""
Billing routes for STR Optimizer.

Wire into app.py with:

    from routes_billing import billing_bp
    app.register_blueprint(billing_bp)

and delete the old create_checkout_session / payment_success /
get_optimization_result handlers.
"""

import logging

from flask import Blueprint, request, jsonify

from services import billing, db

log = logging.getLogger(__name__)

billing_bp = Blueprint("billing", __name__)


@billing_bp.route("/api/create-subscription-session", methods=["POST"])
def create_subscription_session():
    data = request.get_json(silent=True) or {}
    try:
        session = billing.create_subscription_session(
            plan=data.get("plan"),
            email=data.get("email"),
            listing_url=data.get("listingUrl") or data.get("listing_url"),
        )
        return jsonify({"checkout_url": session.url})

    except billing.BillingError as e:
        # Safe to show the user: these are validation messages we wrote.
        return jsonify({"error": str(e)}), 400

    except Exception:
        # Never leak str(e) or a traceback to the client.
        log.exception("create-subscription-session failed")
        return jsonify({"error": "We couldn't start checkout. Please try again."}), 500


@billing_bp.route("/api/stripe/webhook", methods=["POST"])
def stripe_webhook():
    # request.get_data() -- the RAW body. Do not use request.json here;
    # re-serialised JSON will not match the signature.
    payload = request.get_data()
    sig = request.headers.get("Stripe-Signature", "")

    try:
        event = billing.verify_webhook(payload, sig)
    except ValueError:
        log.warning("webhook: malformed payload")
        return jsonify({"error": "invalid payload"}), 400
    except Exception as e:
        # Includes SignatureVerificationError. 400 tells Stripe not to retry
        # something that can never succeed.
        log.warning("webhook: signature verification failed: %s", e)
        return jsonify({"error": "invalid signature"}), 400

    # Stripe retries on any non-2xx, and can deliver the same event twice
    # even on success. Claim it once.
    if db.event_already_processed(event["id"]):
        log.info("webhook: duplicate %s ignored", event["id"])
        return jsonify({"status": "duplicate"}), 200

    try:
        result = billing.handle_event(event)
        db.mark_event_type(event["id"], event["type"])
        log.info("webhook %s: %s", event["type"], result)
    except Exception:
        log.exception("webhook handler failed for %s", event["id"])
        # 500 asks Stripe to retry. The event id row stays, so add
        # compensating logic if you ever want a true retry here.
        return jsonify({"error": "handler failed"}), 500

    return jsonify({"status": "ok"}), 200


@billing_bp.route("/api/subscription-status", methods=["POST"])
def subscription_status():
    """Lightweight check the frontend can use after checkout."""
    data = request.get_json(silent=True) or {}
    try:
        email = billing.validate_email(data.get("email"))
    except billing.BillingError as e:
        return jsonify({"error": str(e)}), 400

    rows = db.get_active_by_email(email)
    return jsonify({
        "active": len(rows) > 0,
        "listings": [
            {
                "listing_url": r["listing_url"],
                "plan": r["plan"],
                "status": r["status"],
                "cancel_at_period_end": r["cancel_at_period_end"],
                "current_period_end": (
                    r["current_period_end"].isoformat() if r["current_period_end"] else None
                ),
            }
            for r in rows
        ],
    })
