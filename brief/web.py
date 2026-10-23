"""One-click unsubscribe (RFC 8058). Stops emails; billing is managed in the Stripe portal."""
from __future__ import annotations

import html

from flask import Blueprint, request

from services import db

from .send import verify_unsubscribe_token

bp = Blueprint("brief", __name__)
PORTAL = "https://billing.stripe.com/p/login/bJe8wR0Kg8VA9YpfJ44ow00"

_PAGE = """<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>STR Optimizer</title><body style="font-family:Helvetica,Arial,sans-serif;max-width:480px;margin:60px auto;padding:0 16px;color:#222">
<h1 style="font-size:22px">{title}</h1><p style="line-height:1.5">{body}</p></body>"""


@bp.route("/api/unsubscribe", methods=["GET", "POST"])
def unsubscribe():
    email = verify_unsubscribe_token(request.args.get("t", ""))
    if not email:
        return _PAGE.format(title="That link didn't work",
                            body="Reply to any brief with “unsubscribe” and we'll take care of it."), 400
    if request.method == "POST":  # mail client one-click, or the confirm button below
        db.unsubscribe(email)
        if request.form.get("List-Unsubscribe") == "One-Click":
            return "", 200
        return _PAGE.format(
            title="You're unsubscribed",
            body=f"We won't email {html.escape(email)} any more briefs. This doesn't cancel billing: "
                 f'<a href="{PORTAL}">manage or cancel your subscription here</a>.')
    # GET only shows a button: link scanners in mail filters must not unsubscribe people.
    return _PAGE.format(
        title="Stop sending briefs?",
        body=f'<form method="post">Stop emailing {html.escape(email)}? '
             f'<button type="submit" style="margin-top:12px;padding:10px 16px">Unsubscribe</button></form>')
