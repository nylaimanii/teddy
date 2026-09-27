"""Mood check-ins: label + timestamp only. No face images are ever stored or sent anywhere.

senses/ smooths the face emotion (vision.mood()); here it becomes a kid-friendly feeling, goes to
Tiger Data (every reading) and Snowflake (batched), and an upset streak of 10+ seconds asks the agent
for a gentle check-in. This is a mood check-in for the parent, never a diagnosis.
"""
import datetime as dt
import threading
import time

from brain import activity, flags
from brain import snowflake as sf

FEELINGS = ["calm", "happy", "sad", "frustrated", "upset"]
MAP = {"neutral": "calm", "happy": "happy", "surprised": "happy", "sad": "sad", "angry": "frustrated",
       "fearful": "upset", "disgusted": "frustrated", "contempt": "frustrated"}
UPSET = {"sad", "frustrated", "upset"}
CHECKIN_AFTER_S = 10
MOMENT_MIN_S = 5       # shorter blips aren't worth showing a parent
COOLDOWN_S = 300       # at most one check-in every 5 minutes
GAP_S = 3              # face out of view this long ends a streak

_buf, _buf_lock = [], threading.Lock()


def feeling(raw):
    return MAP.get((raw or "").lower())


def record(raw, conf=None, at=None):
    """One smoothed reading from senses -> Tiger (time-series) + Snowflake MOODS (batched)."""
    label = feeling(raw)
    if not label:
        return None
    now = at or dt.datetime.now().replace(microsecond=0)
    doing = activity.current()
    if flags.tiger():
        from brain import tiger
        tiger.log_mood(label, conf, doing, at=now.astimezone(dt.timezone.utc) if now.tzinfo else None)
    with _buf_lock:
        _buf.append((sf._ts(now) if not now.tzinfo else sf._ts(now.replace(tzinfo=None)), flags.KID_NAME,
                     label, raw, conf, doing))
    return label


def _flush_loop():
    while True:
        time.sleep(15)
        with _buf_lock:
            rows, _buf[:] = list(_buf), []
        if rows:
            b = sf.backend()
            try:
                b.query("CREATE TABLE IF NOT EXISTS MOODS (TS TIMESTAMP_NTZ, KID STRING, LABEL STRING, RAW STRING, "
                        "CONF FLOAT, ACTIVITY STRING)" if b.name == "snowflake" else
                        "CREATE TABLE IF NOT EXISTS MOODS (TS TEXT, KID TEXT, LABEL TEXT, RAW TEXT, CONF REAL, ACTIVITY TEXT)")
                b.query("INSERT INTO MOODS (TS, KID, LABEL, RAW, CONF, ACTIVITY) VALUES "
                        + ",".join(["(%s,%s,%s,%s,%s,%s)"] * len(rows)), [v for r in rows for v in r])
            except Exception as e:
                print(f"[mood] snowflake write failed ({len(rows)} rows): {e}")


threading.Thread(target=_flush_loop, daemon=True, name="mood-flush").start()


class UpsetTracker:
    """Feed it readings; it says when to check in and records each upset moment when it ends."""

    def __init__(self):
        self.start = self.last = None
        self.labels = []
        self.doing = None
        self.checked_in = False
        self.cooldown_until = 0

    def update(self, label, now=None):
        now = now or time.time()
        if label in UPSET:
            if self.start is None:
                self.start, self.labels, self.checked_in, self.doing = now, [], False, activity.current()
            self.last = now
            self.labels.append(label)
            if not self.checked_in and now - self.start >= CHECKIN_AFTER_S and now >= self.cooldown_until:
                self.checked_in = True
                self.cooldown_until = now + COOLDOWN_S
                return "checkin"
        elif self.start is not None and (label is not None or now - (self.last or now) > GAP_S):
            self._end()
        return None

    def _end(self):
        seconds = round((self.last or self.start) - self.start)
        if seconds >= MOMENT_MIN_S:
            main = max(set(self.labels), key=self.labels.count)
            activity.log("upset_moment", skill=self.doing, duration_s=seconds, feeling=main,
                         started=dt.datetime.fromtimestamp(self.start).isoformat(timespec="seconds"),
                         helping_with=self.doing or "hanging out", checked_in=self.checked_in)
        self.start = self.last = None
        self.labels = []
