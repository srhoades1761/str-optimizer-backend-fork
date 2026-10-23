"""
Stripe subscription billing for STR Optimizer.

Two rules this module exists to enforce:

  1. The amount is NEVER taken from the client. The browser sends a plan
     key ('monthly_499' | 'annual_4788'); the server maps that to a Price
     ID it controls. A tampered request can't buy a $47.88 plan for $0.01.

  2. Fulfilment is driven by webhooks, not by the browser reaching a
     success URL. A customer who pays and closes the tab still gets their
     report.
"""

import os
import re
import logging
from urllib.parse import urlparse

import stripe

from . import db

log = logging.getLogger(__name__)

stripe.api_key = os.getenv("STRIPE_SECRET_KEY")

# Plan key -> Price ID. Set these as Heroku config vars; never hardcode,
# so test and live can differ without a code change.
PRICE_IDS = {
    "monthly_499": os.getenv("STRIPE_PRICE_MONTHLY"),
    "annual_4788": os.getenv("STRIPE_PRICE_ANNUAL"),
}

WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")
FRONTEND_URL = os.getenv("FRONTEND_URL", "https://optimizemystr.com")

# Airbnb only: the brief reads Airbnb's listing page format. VRBO and
# Booking.com are refused at checkout rather than charged and left unserved.
ALLOWED_LISTING_HOSTS = (
    "airbnb.com", "airbnb.co.uk", "airbnb.ca", "airbnb.com.au",
)

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[a-zA-Z]{2,}$")


class BillingError(Exception):
    """Raised for input we refuse; the message is safe to show a user."""


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------

def validate_email(email):
    email = (email or "").strip()
    if not EMAIL_RE.match(email):
        raise BillingError("Please enter a valid email address.")
    return email


def validate_listing_url(url):
    """Accept only public listing pages on hosts we actually support.

    This is also the SSRF guard: the URL is later fetched server-side, so
    an unvalidated value could be pointed at internal addresses such as
    the cloud metadata endpoint.
    """
    url = (url or "").strip()
    if not url:
        raise BillingError("Please paste your listing URL.")

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise BillingError("Listing URL must start with https://")

    host = (parsed.hostname or "").lower().lstrip(".")
    if not any(host == h or host.endswith("." + h) for h in ALLOWED_LISTING_HOSTS):
        raise BillingError("That doesn't look like an Airbnb listing URL.")
    if not re.match(r"^/rooms/(?:plus/)?\d+", parsed.path or ""):
        raise BillingError("Please paste the link to your listing page (it contains /rooms/).")
    return url


# --------------------------------------------------------------------------
# Checkout
# --------------------------------------------------------------------------

def create_subscription_session(plan, email, listing_url):
    """Create a Stripe Checkout Session in subscription mode."""
    if not stripe.api_key:
        raise BillingError("Payment processing isn't configured. Please contact support.")

    price_id = PRICE_IDS.get(plan)
    if not price_id:
        # Unknown plan key, or the config var is missing on this dyno.
        log.error("No price configured for plan=%r (known: %s)", plan, list(PRICE_IDS))
        raise BillingError("That plan isn't available. Please refresh and try again.")

    email = validate_email(email)
    listing_url = validate_listing_url(listing_url)

    session = stripe.checkout.Session.create(
        mode="subscription",
        line_items=[{"price": price_id, "quantity": 1}],

        # Attaches the address to the Customer Stripe creates. The no-code
        # customer portal looks customers up BY EMAIL, so without this the
        # "Manage plan" link silently fails for that subscriber.
        customer_email=email,

        # Session metadata does not propagate to the Subscription, so set
        # both. subscription_data.metadata is what later webhooks carry.
        metadata={"plan": plan, "listing_url": listing_url, "email": email},
        subscription_data={
            "metadata": {"plan": plan, "listing_url": listing_url, "email": email}
        },

        success_url=f"{FRONTEND_URL}/welcome",
        cancel_url=f"{FRONTEND_URL}/#pricing",

        allow_promotion_codes=False,  # protects the retention-coupon strategy
        client_reference_id=listing_url[:200],
    )
    log.info("checkout session created id=%s plan=%s", session.id, plan)
    return session


def create_portal_session(customer_id):
    """Server-side portal session.

    Only needed if you later replace the no-code login link with an
    in-app 'Manage plan' button for a signed-in user.
    """
    return stripe.billing_portal.Session.create(
        customer=customer_id, return_url=FRONTEND_URL
    )


# --------------------------------------------------------------------------
# Webhooks
# --------------------------------------------------------------------------

def verify_webhook(payload_bytes, signature_header):
    """Verify a webhook signature and return the Event.

    payload_bytes MUST be the raw request body. Parsing to JSON first and
    re-serialising changes the bytes and the signature will never match --
    the single most common cause of 'webhook works locally, 400s in
    production'.
    """
    if not WEBHOOK_SECRET:
        raise BillingError("STRIPE_WEBHOOK_SECRET is not set")
    return stripe.Webhook.construct_event(
        payload_bytes, signature_header, WEBHOOK_SECRET
    )


def _period_end(sub):
    """current_period_end moved onto subscription items in recent API versions."""
    end = sub.get("current_period_end")
    if end:
        return end
    try:
        return sub["items"]["data"][0].get("current_period_end")
    except (KeyError, IndexError, TypeError):
        return None


def _as_dict(obj):
    """Normalise a StripeObject to a plain dict.

    stripe.Webhook.construct_event returns StripeObject instances, which
    support subscripting but NOT .get(). Everything downstream expects
    plain dicts, so convert once here -- otherwise the handlers work in
    unit tests (plain dicts) and raise AttributeError in production.
    """
    if isinstance(obj, dict):
        return obj
    for attr in ("to_dict_recursive", "to_dict"):
        fn = getattr(obj, attr, None)
        if callable(fn):
            try:
                return fn()
            except Exception:
                pass
    try:
        return dict(obj)
    except Exception:
        return obj


def handle_event(event):
    """Dispatch one verified Stripe event.

    Returns a short string for logging. Must stay fast -- Stripe times out
    a webhook around 20s and retries, so no report generation happens here.
    """
    etype = event["type"]
    obj = _as_dict(event["data"]["object"])

    if etype == "checkout.session.completed":
        return _on_checkout_completed(obj)
    if etype in ("customer.subscription.updated", "customer.subscription.created"):
        return _on_subscription_updated(obj)
    if etype == "customer.subscription.deleted":
        return _on_subscription_deleted(obj)
    if etype == "invoice.paid":
        return _on_invoice_paid(obj)
    if etype == "invoice.payment_failed":
        return _on_invoice_failed(obj)

    return f"ignored {etype}"


def _on_checkout_completed(session):
    # A subscription checkout can complete before payment settles for some
    # payment methods. Only provision once it's actually paid.
    if session.get("payment_status") not in ("paid", "no_payment_required"):
        return f"checkout {session.get('id')} not paid yet"

    meta = session.get("metadata") or {}
    email = (meta.get("email")
             or (session.get("customer_details") or {}).get("email")
             or session.get("customer_email"))
    listing_url = meta.get("listing_url")
    if not email or not listing_url:
        log.error("checkout.session.completed missing metadata: %s", session.get("id"))
        return "missing metadata"

    row = db.upsert_subscriber(
        email=email,
        listing_url=listing_url,
        stripe_customer_id=session.get("customer"),
        stripe_subscription_id=session.get("subscription"),
        plan=meta.get("plan"),
        status="active",
    )
    db.enqueue_report(row["id"], kind="initial")
    log.info("subscriber %s activated, initial report queued", row["id"])
    return f"activated subscriber {row['id']}"


def _on_subscription_updated(sub):
    plan = None
    try:
        plan = (sub["items"]["data"][0]["price"]["id"])
    except (KeyError, IndexError, TypeError):
        pass
    plan_key = next((k for k, v in PRICE_IDS.items() if v and v == plan), None)

    cancel_details = sub.get("cancellation_details") or {}

    updated = db.update_by_subscription_id(
        sub["id"],
        status=sub.get("status"),
        cancel_at_period_end=bool(sub.get("cancel_at_period_end")),
        current_period_end=_to_ts(_period_end(sub)),
        cancellation_reason=cancel_details.get("feedback") or cancel_details.get("comment"),
        **({"plan": plan_key} if plan_key else {}),
    )
    return f"subscription {sub['id']} -> {sub.get('status')}" if updated else "unknown subscription"


def _on_subscription_deleted(sub):
    cancel_details = sub.get("cancellation_details") or {}
    db.update_by_subscription_id(
        sub["id"],
        status="canceled",
        cancel_at_period_end=False,
        cancellation_reason=cancel_details.get("feedback") or cancel_details.get("comment"),
    )
    return f"subscription {sub['id']} canceled"


def _on_invoice_paid(invoice):
    sub_id = _invoice_subscription_id(invoice)
    if not sub_id:
        return "invoice without subscription"
    db.update_by_subscription_id(sub_id, status="active")
    return f"invoice paid for {sub_id}"


def _on_invoice_failed(invoice):
    sub_id = _invoice_subscription_id(invoice)
    if not sub_id:
        return "invoice without subscription"
    db.update_by_subscription_id(sub_id, status="past_due")
    return f"invoice failed for {sub_id}"


def _invoice_subscription_id(invoice):
    """Subscription id moved location across API versions; check both."""
    sub = invoice.get("subscription")
    if isinstance(sub, str):
        return sub
    if isinstance(sub, dict):
        return sub.get("id")
    try:
        for line in invoice["lines"]["data"]:
            parent = line.get("parent") or {}
            details = parent.get("subscription_item_details") or {}
            if details.get("subscription"):
                return details["subscription"]
            if line.get("subscription"):
                return line["subscription"]
    except (KeyError, TypeError):
        pass
    return None


def _to_ts(epoch):
    if not epoch:
        return None
    from datetime import datetime, timezone
    return datetime.fromtimestamp(epoch, tz=timezone.utc)
