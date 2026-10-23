#!/usr/bin/env python
"""
Brief worker for STR Optimizer.

The Stripe webhook only writes a row (subscriber + report_job). This process
does the slow part: read the listing, pull market data, write the copy,
render the brief and email it. Stripe times webhooks out at ~20s, so none of
this can happen inside the webhook.

    python worker.py --loop             # Render background worker
    python worker.py --once             # drain the queue and exit
    python worker.py --enqueue-monthly  # Render cron job, once a day
"""

import argparse
import logging
import os
import sys
import time

from services import db

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("worker")

POLL_SECONDS = int(os.getenv("WORKER_POLL_SECONDS", "20"))


def _build_and_send(job, subscriber):
    """Returns the brief context's summary. Imported lazily so the worker boots
    and logs a clear error even if a dependency is missing."""
    from brief.cache import DbMarketCache
    from brief.pipeline import build_brief
    from brief.send import send_brief, unsubscribe_url

    html, ctx = build_brief(
        subscriber["listing_url"], subscriber["email"],
        is_first=(job["kind"] == "initial"),
        unsubscribe_url=unsubscribe_url(subscriber["email"]),
        market_cache=DbMarketCache(db),
    )
    message_id = send_brief(subscriber["email"], html, ctx)
    return {"message_id": message_id, "moves": ctx.get("_move_keys", []),
            "market": ctx.get("market") is not None}


def run_once(build_and_send=None):
    """Process a single job. Returns True if one was claimed."""
    from brief.listing import ListingError

    build_and_send = build_and_send or _build_and_send
    job = db.claim_next_job()
    if not job:
        return False

    log.info("job %s (%s) claimed, attempt %s", job["id"], job["kind"], job["attempts"])
    try:
        with db.get_cursor() as cur:
            cur.execute("SELECT * FROM subscribers WHERE id = %s", (job["subscriber_id"],))
            subscriber = cur.fetchone()
        if not subscriber:
            raise RuntimeError(f"subscriber {job['subscriber_id']} vanished")

        # Don't spend API budget on someone who cancelled or opted out of email.
        if subscriber["status"] not in ("active", "trialing"):
            log.warning("job %s skipped: subscriber status=%s", job["id"], subscriber["status"])
            db.finish_job(job["id"], summary={"skipped": subscriber["status"]})
            return True
        if db.is_unsubscribed(subscriber["email"]):
            log.info("job %s skipped: unsubscribed", job["id"])
            db.finish_job(job["id"], summary={"skipped": "unsubscribed"})
            return True

        summary = build_and_send(job, subscriber)
        db.finish_job(job["id"], summary=summary)
        log.info("job %s sent", job["id"])

    except ListingError as e:
        # Private, deleted or unreadable listing: retrying won't help.
        # The failed row is the support follow-up list.
        log.warning("job %s listing error: %s", job["id"], e)
        db.fail_job_permanently(job["id"], f"listing: {e}")
    except Exception as e:
        log.exception("job %s failed", job["id"])
        db.fail_job(job["id"], e)

    return True


def loop():
    log.info("worker started, polling every %ss", POLL_SECONDS)
    while True:
        try:
            worked = run_once()
        except Exception:
            log.exception("worker loop error")
            worked = False
        if not worked:
            time.sleep(POLL_SECONDS)


def enqueue_monthly():
    rows = db.due_for_monthly_report()
    for r in rows:
        db.enqueue_report(r["id"], kind="monthly")
    log.info("queued %s monthly briefs", len(rows))
    return len(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", action="store_true", help="run continuously")
    ap.add_argument("--once", action="store_true", help="drain pending jobs, then exit")
    ap.add_argument("--enqueue-monthly", action="store_true", help="queue monthly briefs that are due")
    args = ap.parse_args()

    db.init_pool()
    db.ensure_schema()

    if args.enqueue_monthly:
        enqueue_monthly()
    elif args.loop:
        loop()
    elif args.once:
        n = 0
        while run_once():
            n += 1
        log.info("drained %s jobs", n)
    else:
        ap.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
