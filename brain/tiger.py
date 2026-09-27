"""Tiger Data (TimescaleDB): the time-series log behind the Parent charts.

    TIGER_DATABASE_URL=postgres://user:pass@host:port/tsdb?sslmode=require   (from the Tiger Cloud console)
    python -m brain.tiger --setup     # hypertables + continuous aggregates + retention

Hypertables
  teddy_activity(time, kid, kind, skill, duration_s, detail)   every session, step, skill, find, homework try
  teddy_moods(time, kid, label, conf, activity)                mood check-in labels only (no images), ~1/s
Continuous aggregates (what the Parent page reads)
  practice_daily   minutes + sessions per skill per day
  moods_daily      readings per feeling per day
Moods are kept 90 days (retention policy); the parent page only ever shows labels and times.
"""
import datetime as dt
import json
import os
import queue
import sys
import threading

from brain import flags

TZ = os.getenv("TEDDY_TZ", "America/New_York")  # days are the family's days, not UTC

_conn = None
_lock = threading.RLock()
_moods = queue.Queue()


def conn():
    global _conn
    import psycopg
    with _lock:
        if _conn is None or _conn.closed:
            _conn = psycopg.connect(os.environ["TIGER_DATABASE_URL"], autocommit=True, connect_timeout=10)
        return _conn


def q(sql, params=None):
    with _lock:
        c = conn()
    with c.cursor() as cur:
        cur.execute(sql, params)
        if cur.description is None:
            return []
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


SETUP = [
    "CREATE EXTENSION IF NOT EXISTS timescaledb",
    """CREATE TABLE IF NOT EXISTS teddy_activity (
         time timestamptz NOT NULL, kid text NOT NULL, kind text NOT NULL, skill text,
         duration_s double precision, detail jsonb)""",
    "SELECT create_hypertable('teddy_activity', by_range('time', INTERVAL '1 day'), if_not_exists => TRUE)",
    "CREATE INDEX IF NOT EXISTS teddy_activity_kid_kind ON teddy_activity (kid, kind, time DESC)",
    """CREATE TABLE IF NOT EXISTS teddy_moods (
         time timestamptz NOT NULL, kid text NOT NULL, label text NOT NULL, conf real, activity text)""",
    "SELECT create_hypertable('teddy_moods', by_range('time', INTERVAL '1 day'), if_not_exists => TRUE)",
    "SELECT add_retention_policy('teddy_moods', INTERVAL '90 days', if_not_exists => TRUE)",
    """CREATE MATERIALIZED VIEW IF NOT EXISTS practice_daily WITH (timescaledb.continuous) AS
         SELECT time_bucket('1 day', time, '{tz}') AS day, kid, skill,
                count(*) FILTER (WHERE kind IN ('howto_start', 'homework_start')) AS sessions,
                count(*) FILTER (WHERE kind = 'skill_done') AS finished,
                coalesce(sum(duration_s) FILTER (WHERE kind IN ('skill_done', 'homework_done')), 0) / 60.0 AS minutes
         FROM teddy_activity WHERE skill IS NOT NULL GROUP BY 1, 2, 3 WITH NO DATA""",
    """SELECT add_continuous_aggregate_policy('practice_daily', start_offset => INTERVAL '30 days',
         end_offset => NULL, schedule_interval => INTERVAL '5 minutes', if_not_exists => TRUE)""",
    """CREATE MATERIALIZED VIEW IF NOT EXISTS moods_daily WITH (timescaledb.continuous) AS
         SELECT time_bucket('1 day', time, '{tz}') AS day, kid, label, count(*) AS readings
         FROM teddy_moods GROUP BY 1, 2, 3 WITH NO DATA""",
    """SELECT add_continuous_aggregate_policy('moods_daily', start_offset => INTERVAL '30 days',
         end_offset => NULL, schedule_interval => INTERVAL '5 minutes', if_not_exists => TRUE)""",
    # real-time aggregates: today's rows show up before the policy refreshes
    "ALTER MATERIALIZED VIEW practice_daily SET (timescaledb.materialized_only = false)",
    "ALTER MATERIALIZED VIEW moods_daily SET (timescaledb.materialized_only = false)",
]


def setup():
    for stmt in SETUP:
        q(stmt.replace("{tz}", TZ))
    refresh()


def refresh():
    for view in ("practice_daily", "moods_daily"):
        with _lock:
            c = conn()
        c.execute(f"CALL refresh_continuous_aggregate('{view}', NULL, NULL)")


# ---------------------------------------------------------------- writes
def log_activity(kind, skill=None, duration_s=None, detail=None, kid=None, at=None):
    try:
        q("INSERT INTO teddy_activity (time, kid, kind, skill, duration_s, detail) VALUES (%s,%s,%s,%s,%s,%s)",
          (at or dt.datetime.now(dt.timezone.utc), kid or flags.KID_NAME, kind, skill, duration_s,
           json.dumps(detail or {}, default=str)))
    except Exception as e:
        print(f"[tiger] activity write failed: {e}")


def log_mood(label, conf=None, activity=None, kid=None, at=None):
    """Buffered: mood readings arrive about once a second."""
    _moods.put((at or dt.datetime.now(dt.timezone.utc), kid or flags.KID_NAME, label, conf, activity))


def _mood_writer():
    import time
    while True:
        rows = [_moods.get()]
        time.sleep(3)
        while not _moods.empty():
            rows.append(_moods.get_nowait())
        try:
            with _lock:
                c = conn()
            with c.cursor() as cur:
                cur.executemany("INSERT INTO teddy_moods (time, kid, label, conf, activity) VALUES (%s,%s,%s,%s,%s)", rows)
        except Exception as e:
            print(f"[tiger] mood write failed ({len(rows)} rows): {e}")


threading.Thread(target=_mood_writer, daemon=True, name="tiger-moods").start()


# ---------------------------------------------------------------- parent charts
def practice_by_day(days=7, kid=None):
    return q(f"""SELECT (day AT TIME ZONE '{TZ}')::date AS day, skill, sessions, finished, round(minutes::numeric, 1) AS minutes
                FROM practice_daily WHERE kid = %s AND day > now() - make_interval(days => %s)
                ORDER BY day, skill""", (kid or flags.KID_NAME, days))


def moods_by_day(days=7, kid=None):
    return q(f"""SELECT (day AT TIME ZONE '{TZ}')::date AS day, label, readings FROM moods_daily
                WHERE kid = %s AND day > now() - make_interval(days => %s) ORDER BY day, label""",
             (kid or flags.KID_NAME, days))


def events(kinds, days=7, kid=None, limit=200):
    return q("""SELECT time, kind, skill, duration_s, detail FROM teddy_activity
                WHERE kid = %s AND kind = ANY(%s) AND time > now() - make_interval(days => %s)
                ORDER BY time DESC LIMIT %s""", (kid or flags.KID_NAME, list(kinds), days, limit))


if __name__ == "__main__":
    if not flags.tiger():
        sys.exit("Set TIGER_DATABASE_URL in .env (Tiger Cloud console -> your service -> Connect).")
    if "--setup" in sys.argv:
        setup()
        print("Tiger Data ready: teddy_activity, teddy_moods (+ practice_daily, moods_daily, 90-day mood retention)")
    print(q("SELECT (SELECT count(*) FROM teddy_activity) AS activity_rows, (SELECT count(*) FROM teddy_moods) AS mood_rows"))
