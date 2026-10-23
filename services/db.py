"""
Postgres data layer for subscribers, webhook idempotency and report jobs.

Uses a small connection pool so a single sync gunicorn worker doesn't
open a new TCP connection per request. Heroku Postgres has a low
connection cap on the cheaper plans, so keep maxconn modest.
"""

import os
import logging
from contextlib import contextmanager

import psycopg2
import psycopg2.extras
from psycopg2 import pool as pgpool

log = logging.getLogger(__name__)

_POOL = None


def _dsn():
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        raise RuntimeError("DATABASE_URL is not set")
    # Heroku still hands out the deprecated postgres:// scheme.
    if dsn.startswith("postgres://"):
        dsn = dsn.replace("postgres://", "postgresql://", 1)
    return dsn


def init_pool(minconn=1, maxconn=4):
    global _POOL
    if _POOL is None:
        _POOL = pgpool.ThreadedConnectionPool(
            minconn, maxconn, dsn=_dsn(), sslmode=os.getenv("PGSSLMODE", "require")
        )
    return _POOL


SCHEMA_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "schema.sql")


def ensure_schema():
    """Apply schema.sql. Every statement is IF NOT EXISTS, so this is safe on every boot
    and removes the manual psql step from deployment."""
    with open(SCHEMA_PATH) as f:
        sql = f.read()
    with get_cursor(commit=True) as cur:
        cur.execute("SELECT pg_advisory_xact_lock(812734)")  # web and worker may boot together
        cur.execute(sql)


@contextmanager
def get_cursor(commit=False):
    """Borrow a connection, yield a dict cursor, always return it to the pool."""
    if _POOL is None:
        init_pool()
    conn = _POOL.getconn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            yield cur
        if commit:
            conn.commit()
        else:
            conn.rollback()
    except Exception:
        conn.rollback()
        raise
    finally:
        _POOL.putconn(conn)


# --------------------------------------------------------------------------
# Idempotency
# --------------------------------------------------------------------------

def event_already_processed(event_id):
    """True if we've handled this Stripe event before.

    ON CONFLICT DO NOTHING makes the check-and-claim a single atomic
    statement, so two concurrent deliveries of the same event can't both
    win the race.
    """
    with get_cursor(commit=True) as cur:
        cur.execute(
            """
            INSERT INTO processed_events (event_id, event_type)
            VALUES (%s, %s)
            ON CONFLICT (event_id) DO NOTHING
            RETURNING event_id
            """,
            (event_id, "pending"),
        )
        return cur.fetchone() is None


def mark_event_type(event_id, event_type):
    with get_cursor(commit=True) as cur:
        cur.execute(
            "UPDATE processed_events SET event_type = %s WHERE event_id = %s",
            (event_type, event_id),
        )


# --------------------------------------------------------------------------
# Subscribers
# --------------------------------------------------------------------------

def upsert_subscriber(email, listing_url, **fields):
    """Create or update the subscriber row for (email, listing_url)."""
    cols = ["email", "listing_url"]
    vals = [email, listing_url]
    for k, v in fields.items():
        if v is not None:
            cols.append(k)
            vals.append(v)

    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols[2:]) or "email = EXCLUDED.email"
    sql = f"""
        INSERT INTO subscribers ({", ".join(cols)})
        VALUES ({", ".join(["%s"] * len(vals))})
        ON CONFLICT (LOWER(email), listing_url) DO UPDATE
          SET {updates}, updated_at = NOW()
        RETURNING *
    """
    with get_cursor(commit=True) as cur:
        cur.execute(sql, vals)
        return cur.fetchone()


def update_by_subscription_id(subscription_id, **fields):
    if not fields:
        return None
    sets = ", ".join(f"{k} = %s" for k in fields)
    vals = list(fields.values()) + [subscription_id]
    with get_cursor(commit=True) as cur:
        cur.execute(
            f"UPDATE subscribers SET {sets}, updated_at = NOW() "
            f"WHERE stripe_subscription_id = %s RETURNING *",
            vals,
        )
        return cur.fetchone()


def get_by_subscription_id(subscription_id):
    with get_cursor() as cur:
        cur.execute(
            "SELECT * FROM subscribers WHERE stripe_subscription_id = %s",
            (subscription_id,),
        )
        return cur.fetchone()


def get_active_by_email(email):
    with get_cursor() as cur:
        cur.execute(
            """
            SELECT * FROM subscribers
            WHERE LOWER(email) = LOWER(%s)
              AND status IN ('active', 'trialing', 'past_due')
            ORDER BY created_at DESC
            """,
            (email,),
        )
        return cur.fetchall()


def has_active_subscription(email):
    return len(get_active_by_email(email)) > 0


def due_for_monthly_report():
    """Active subscribers whose last monthly report is 30+ days old."""
    with get_cursor() as cur:
        cur.execute(
            """
            SELECT s.* FROM subscribers s
            WHERE s.status IN ('active', 'trialing')
              AND NOT EXISTS (
                    SELECT 1 FROM report_jobs j
                    WHERE j.subscriber_id = s.id
                      AND j.status IN ('pending', 'running')
              )
              AND COALESCE(
                    (SELECT MAX(finished_at) FROM report_jobs j2
                      WHERE j2.subscriber_id = s.id AND j2.status = 'done'),
                    s.created_at
                  ) < NOW() - INTERVAL '30 days'
            """
        )
        return cur.fetchall()


# --------------------------------------------------------------------------
# Report jobs
# --------------------------------------------------------------------------

def enqueue_report(subscriber_id, kind="initial"):
    with get_cursor(commit=True) as cur:
        cur.execute(
            """
            INSERT INTO report_jobs (subscriber_id, kind)
            SELECT %s, %s
            WHERE NOT EXISTS (
                SELECT 1 FROM report_jobs
                WHERE subscriber_id = %s AND kind = %s
                  AND status IN ('pending', 'running')
            )
            RETURNING *
            """,
            (subscriber_id, kind, subscriber_id, kind),
        )
        return cur.fetchone()


def claim_next_job():
    """Atomically claim one pending job.

    SKIP LOCKED lets several workers run without handing the same job to
    two of them.
    """
    with get_cursor(commit=True) as cur:
        cur.execute(
            """
            UPDATE report_jobs SET status = 'running',
                                   attempts = attempts + 1,
                                   started_at = NOW()
            WHERE id = (
                SELECT id FROM report_jobs
                WHERE status = 'pending' AND attempts < 3
                ORDER BY created_at
                FOR UPDATE SKIP LOCKED
                LIMIT 1
            )
            RETURNING *
            """
        )
        return cur.fetchone()


def finish_job(job_id, pdf_url=None, summary=None):
    with get_cursor(commit=True) as cur:
        cur.execute(
            "UPDATE report_jobs SET status='done', pdf_url=%s, summary=%s, finished_at=NOW() "
            "WHERE id=%s",
            (pdf_url, psycopg2.extras.Json(summary) if summary is not None else None, job_id),
        )


def fail_job_permanently(job_id, error):
    """For errors a retry can't fix (e.g. a private or deleted listing)."""
    with get_cursor(commit=True) as cur:
        cur.execute(
            "UPDATE report_jobs SET status='failed', last_error=%s, finished_at=NOW() WHERE id=%s",
            (str(error)[:2000], job_id),
        )


# --------------------------------------------------------------------------
# Unsubscribes (email preference; billing is managed in the Stripe portal)
# --------------------------------------------------------------------------

def unsubscribe(email):
    with get_cursor(commit=True) as cur:
        cur.execute("INSERT INTO unsubscribes (email) VALUES (LOWER(%s)) ON CONFLICT DO NOTHING", (email,))


def is_unsubscribed(email):
    with get_cursor() as cur:
        cur.execute("SELECT 1 FROM unsubscribes WHERE email = LOWER(%s)", (email,))
        return cur.fetchone() is not None


# --------------------------------------------------------------------------
# Market data cache (AirROI terms: aggregated figures only, refreshed within 30 days)
# --------------------------------------------------------------------------

def cache_get(key):
    with get_cursor() as cur:
        cur.execute("SELECT payload FROM market_cache WHERE key=%s AND fetched_at > NOW() - INTERVAL '30 days'",
                    (key,))
        row = cur.fetchone()
        return row["payload"] if row else None


def cache_set(key, payload):
    with get_cursor(commit=True) as cur:
        cur.execute(
            """INSERT INTO market_cache (key, payload, fetched_at) VALUES (%s, %s, NOW())
               ON CONFLICT (key) DO UPDATE SET payload = EXCLUDED.payload, fetched_at = NOW()""",
            (key, psycopg2.extras.Json(payload)))


def fail_job(job_id, error):
    """Back to pending for another attempt; 'failed' once attempts run out."""
    with get_cursor(commit=True) as cur:
        cur.execute(
            """
            UPDATE report_jobs
               SET status = CASE WHEN attempts >= 3 THEN 'failed' ELSE 'pending' END,
                   last_error = %s,
                   finished_at = CASE WHEN attempts >= 3 THEN NOW() ELSE NULL END
             WHERE id = %s
            """,
            (str(error)[:2000], job_id),
        )
