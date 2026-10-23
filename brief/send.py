"""Email delivery via SendGrid, with one-click unsubscribe (RFC 8058) for Gmail/Yahoo bulk-sender rules.

Requires SPF/DKIM/DMARC on optimizemystr.com (SendGrid domain authentication) before launch.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
from urllib.parse import quote

FROM_EMAIL = os.environ.get("BRIEF_FROM_EMAIL", "briefs@optimizemystr.com")
FROM_NAME = "STR Optimizer"
REPLY_TO = os.environ.get("SUPPORT_EMAIL", "support@optimizemystr.com")
SITE = os.environ.get("FRONTEND_URL", "https://optimizemystr.com").rstrip("/")
API_BASE = os.environ.get("API_BASE_URL", SITE).rstrip("/")


def _secret() -> bytes:
    s = os.environ.get("UNSUBSCRIBE_SECRET")
    if not s:
        raise RuntimeError("UNSUBSCRIBE_SECRET is not set")
    return s.encode()


def unsubscribe_token(email: str) -> str:
    sig = hmac.new(_secret(), email.strip().lower().encode(), hashlib.sha256).digest()[:16]
    return base64.urlsafe_b64encode(email.strip().lower().encode()).decode().rstrip("=") + "." + \
        base64.urlsafe_b64encode(sig).decode().rstrip("=")


def verify_unsubscribe_token(token: str) -> str | None:
    try:
        e, _ = token.split(".", 1)
        email = base64.urlsafe_b64decode(e + "=" * (-len(e) % 4)).decode()
    except Exception:
        return None
    return email if hmac.compare_digest(unsubscribe_token(email), token) else None


def unsubscribe_url(email: str) -> str:
    return f"{API_BASE}/api/unsubscribe?t={quote(unsubscribe_token(email))}"


def subject_for(ctx: dict) -> str:
    name = ctx["property"]["name"]
    return (f"Your first listing brief: {name}" if ctx.get("is_first_brief")
            else f"{name}: your {ctx['period_label']} brief")


def send_brief(to_email: str, html: str, ctx: dict, client=None) -> str:
    """Send and return SendGrid's message id. Raises on failure so the job can retry."""
    from sendgrid import SendGridAPIClient
    from sendgrid.helpers.mail import Header, Mail, ReplyTo

    unsub = ctx.get("unsubscribe_url") or unsubscribe_url(to_email)
    msg = Mail(from_email=(FROM_EMAIL, FROM_NAME), to_emails=to_email,
               subject=subject_for(ctx), html_content=html)
    msg.reply_to = ReplyTo(REPLY_TO)
    msg.add_header(Header("List-Unsubscribe", f"<{unsub}>, <mailto:{REPLY_TO}?subject=unsubscribe>"))
    msg.add_header(Header("List-Unsubscribe-Post", "List-Unsubscribe=One-Click"))
    sg = client or SendGridAPIClient(os.environ["SENDGRID_API_KEY"])
    resp = sg.send(msg)
    if resp.status_code >= 300:
        raise RuntimeError(f"SendGrid returned {resp.status_code}")
    return (resp.headers or {}).get("X-Message-Id", "")
