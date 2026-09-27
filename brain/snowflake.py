"""Teddy's brain + memory, backed by Snowflake.

Contract (CONTRACTS.md):
    log_sighting(label, x, y); last_seen(label); log_event(kind, data); ask(question, domain) -> str

Extras used by agent.py / web/server.py:
    complete(prompt), ask_detailed(), caregiver_ask(), recent_events(), stats(), on_event()

If Snowflake creds are missing in .env (or TEDDY_MOCK=1, or configure(mock=True)) everything
runs against a local SQLite file + Ollama so the demo never dies.
"""
import datetime as dt
import json
import os
import queue
import re
import sqlite3
import threading
import time
from collections import deque
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

TZ = os.getenv("TEDDY_TZ", "America/New_York")
OWNER = os.getenv("TEDDY_OWNER") or (os.getenv("TEDDY_KID", "Maya") if os.getenv("TEDDY_AUDIENCE", "kid") == "kid"
                                     else "Grandma Rose")
OLLAMA_MODEL = os.getenv("TEDDY_OLLAMA_MODEL", "qwen2.5:7b")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
# Open models, best first (confirmed with SHOW CORTEX BASE MODELS). Override with TEDDY_CORTEX_MODEL.
# mistral-large2 is legacy on our account ("please use other models"), so mistral-large3 is the Mistral fallback.
CORTEX_MODELS = [m for m in [os.getenv("TEDDY_CORTEX_MODEL"), "llama3.3-70b", "mistral-large3", "mistral-large2",
                             "llama3.1-8b"] if m]
WAREHOUSE = os.getenv("SNOWFLAKE_WAREHOUSE", "COMPUTE_WH")
SEARCH_SERVICE = "TEDDY.CORE.DOC_SEARCH"
FRAMES_DIR = ROOT / "data" / "frames"
SEMANTIC_MODEL = "@TEDDY.CORE.MODELS/teddy_semantic.yaml"

NOT_SURE = "Hmm, I'm not sure about that one. Let's ask a grown-up."


def _now():
    return dt.datetime.now().replace(microsecond=0)


def _ts(t=None):
    return (t or _now()).strftime("%Y-%m-%d %H:%M:%S")


def _spoken(text):
    """Strip markdown/lists so ElevenLabs reads it naturally."""
    text = re.sub(r"[*_#`>|]+", "", text or "")
    text = re.sub(r"^\s*(\d+[.)]|[-•])\s*", "", text, flags=re.M)
    return re.sub(r"\s+", " ", text).strip().strip('"')


def ollama(prompt, system=None, json_mode=False, temperature=0.4):
    """Local LLM (qwen2.5:7b). Used for personality/intent and as the mock brain."""
    body = {"model": OLLAMA_MODEL, "prompt": prompt, "stream": False,
            "options": {"temperature": temperature, "num_predict": 200}, "keep_alive": "30m"}
    if system:
        body["system"] = system
    if json_mode:
        body["format"] = "json"
    r = requests.post(f"{OLLAMA_URL}/api/generate", json=body, timeout=60)
    r.raise_for_status()
    return r.json()["response"].strip()


# ---------------------------------------------------------------- Snowflake backend
class SnowflakeBackend:
    name = "snowflake"

    def __init__(self):
        self._conn = None
        self._lock = threading.RLock()
        self.model = None

    def conn(self):
        import snowflake.connector
        with self._lock:
            if self._conn is None or self._conn.is_closed():
                self._conn = snowflake.connector.connect(
                    account=os.environ["SNOWFLAKE_ACCOUNT"],
                    user=os.environ["SNOWFLAKE_USER"],
                    password=os.environ["SNOWFLAKE_PASSWORD"],
                    role=os.getenv("SNOWFLAKE_ROLE") or None,
                    warehouse=WAREHOUSE,
                    database="TEDDY", schema="CORE",
                    session_parameters={"TIMEZONE": TZ, "QUERY_TAG": "teddy"},
                    client_session_keep_alive=True, login_timeout=20)
            return self._conn

    def query(self, sql, params=None, timeout=60):
        # One shared connection; separate cursors run concurrently (a slow caregiver
        # question never blocks Teddy's answers or the event writer).
        try:
            cur = self.conn().cursor()
            cur.execute(sql, params, timeout=timeout)
        except Exception as e:  # dropped session -> reconnect once
            if "session" not in str(e).lower() and "connection" not in str(e).lower():
                raise
            with self._lock:
                self._conn = None
            cur = self.conn().cursor()
            cur.execute(sql, params, timeout=timeout)
        if cur.description is None:
            return []
        cols = [c[0].lower() for c in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def insert_events(self, rows):  # rows: [(ts, kind, json_str)]
        for i in range(0, len(rows), 300):
            part = rows[i:i + 300]
            vals = ",".join(["(%s,%s,%s)"] * len(part))
            self.query("INSERT INTO EVENTS (TS, KIND, DATA) SELECT column1::TIMESTAMP_NTZ, column2, "
                       f"PARSE_JSON(column3) FROM VALUES {vals}", [v for r in part for v in r])

    def insert_sightings(self, rows):  # rows: [(ts, label, x, y, w, h, frame_path)]
        for i in range(0, len(rows), 500):
            part = rows[i:i + 500]
            vals = ",".join(["(%s,%s,%s,%s,%s,%s,%s)"] * len(part))
            self.query(f"INSERT INTO SIGHTINGS (TS, LABEL, X, Y, W, H, FRAME_PATH) VALUES {vals}",
                       [v for r in part for v in r])

    def upload_frame(self, path):
        self.query(f"PUT 'file://{path}' @TEDDY.CORE.FRAMES AUTO_COMPRESS=FALSE OVERWRITE=TRUE")

    def complete(self, prompt, temperature=None):
        """AI_COMPLETE with llama3.3-70b, falling back to the next model on error or timeout."""
        models = [self.model] + [m for m in CORTEX_MODELS if m != self.model] if self.model else CORTEX_MODELS
        last = None
        for m in models:
            try:
                if temperature is None:
                    out = self.query("SELECT AI_COMPLETE(%s, %s) AS R", (m, prompt), timeout=25)[0]["r"]
                else:
                    out = self.query("SELECT AI_COMPLETE(%s, %s, OBJECT_CONSTRUCT('temperature', %s::FLOAT)) AS R",
                                     (m, prompt, temperature), timeout=25)[0]["r"]
                self.model = m
                out = out.strip()
                if out.startswith('"'):  # AI_COMPLETE hands back a JSON-encoded string
                    try:
                        out = json.loads(out)
                    except ValueError:
                        pass
                return out.strip()
            except Exception as e:
                print(f"[brain] AI_COMPLETE {m} failed: {str(e).splitlines()[0][:120]}")
                last = e
        raise RuntimeError(f"No Cortex model available: {last}")

    def search(self, question, domain=None, k=5):
        """Cortex Search retrieval -> [{"chunk","title","domain"}] (SEARCH_PREVIEW over DOC_SEARCH)."""
        req = {"query": question, "columns": ["CHUNK", "TITLE", "DOMAIN", "SOURCE_URL"], "limit": k}
        if domain:
            req["filter"] = {"@eq": {"DOMAIN": domain}}
        r = self.query("SELECT SNOWFLAKE.CORTEX.SEARCH_PREVIEW(%s, %s) AS R", (SEARCH_SERVICE, json.dumps(req)),
                       timeout=15)
        hits = json.loads(r[0]["r"]).get("results", [])
        return [{k.lower(): v for k, v in h.items()} for h in hits]

    def run_readonly(self, sql):
        return self.query(sql)

    def analyst_sql(self, question, timeout=15):
        """Cortex Analyst: question -> SQL over the semantic model. None if unavailable."""
        c = self.conn()
        r = requests.post(
            f"https://{c.host}/api/v2/cortex/analyst/message",
            headers={"Authorization": f'Snowflake Token="{c.rest.token}"', "Content-Type": "application/json"},
            json={"messages": [{"role": "user", "content": [{"type": "text", "text": question}]}],
                  "semantic_model_file": SEMANTIC_MODEL},
            timeout=timeout)
        r.raise_for_status()
        for part in r.json().get("message", {}).get("content", []):
            if part.get("type") == "sql":
                return part["statement"]
        return None

    dialect = "Snowflake SQL"
    week_ago = "DATEADD(day, -7, CURRENT_TIMESTAMP())"


# ---------------------------------------------------------------- Local mock backend
_OFFLINE_DOCS = [
    ("cpr", "CPR basics", "Call 911 first, or have someone else call. Put the heel of one hand in the center of the "
     "chest with your other hand on top. Push hard and fast, about 2 inches deep, 100 to 120 times a minute. "
     "Let the chest come all the way back up between pushes. Keep going until help arrives or an AED is ready."),
    ("cpr", "AED", "Turn on the AED and follow its voice. Stick the pads on the bare chest as shown in the pictures. "
     "Nobody touches the person while it checks the heart or gives a shock."),
    ("first_aid", "Cuts", "For a bleeding cut, press firmly on it with a clean cloth for 10 minutes. Once it stops, "
     "rinse with clean water and cover with a bandage. Call 911 if blood spurts or will not stop."),
    ("first_aid", "Burns", "Cool a burn under cool running water for 20 minutes. Do not use ice or butter. "
     "Cover loosely with a clean cloth. Get help if the burn is large, deep, or on the face."),
    ("first_aid", "Falls", "After a fall, stay still for a moment and check for pain. Do not rush to stand. "
     "If you hit your head, feel dizzy, or cannot get up, call for help and stay warm until someone comes."),
    ("first_aid", "Choking", "If someone cannot cough, talk, or breathe, call 911. Give 5 back blows between the "
     "shoulder blades, then 5 abdominal thrusts, and repeat until the object comes out."),
    ("homework", "Fractions", "A fraction shows part of a whole. The bottom number says how many equal parts, "
     "and the top number says how many parts you have. To add fractions with the same bottom, add the tops."),
    ("homework", "Photosynthesis", "Plants make their own food with photosynthesis. They use sunlight, water, and "
     "carbon dioxide from the air to make sugar, and they give off oxygen."),
]


class LocalBackend:
    name = "local"
    dialect = "SQLite"
    week_ago = "datetime('now','localtime','-7 days')"

    def __init__(self, path=ROOT / "data" / "teddy_local.db"):
        self.path = str(path)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        cols = [r[1] for r in self._db.execute("PRAGMA table_info(SIGHTINGS)")]
        if cols and "FRAME_PATH" not in cols:  # old mock schema -> start fresh
            self._db.executescript("DROP TABLE SIGHTINGS; DROP TABLE IF EXISTS EVENTS; DROP VIEW IF EXISTS EVENTS_FLAT;")
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS SIGHTINGS (LABEL TEXT, X REAL, Y REAL, W REAL, H REAL, FRAME_PATH TEXT, TS TEXT);
            CREATE TABLE IF NOT EXISTS EVENTS (KIND TEXT, DATA TEXT, TS TEXT);
            CREATE VIEW IF NOT EXISTS EVENTS_FLAT AS SELECT TS, date(TS) AS DAY, KIND,
              json_extract(DATA,'$.source') AS SOURCE, json_extract(DATA,'$.intent') AS INTENT,
              json_extract(DATA,'$.object') AS OBJECT, json_extract(DATA,'$.score') AS MOOD_SCORE,
              json_extract(DATA,'$.mood') AS MOOD, json_extract(DATA,'$.status') AS STATUS,
              json_extract(DATA,'$.heart_rate') AS HEART_RATE, json_extract(DATA,'$.text') AS TEXT,
              json_extract(DATA,'$.reply') AS REPLY FROM EVENTS;
        """)

    def query(self, sql, params=None):
        sql = sql.replace("%s", "?")
        with self._lock:
            cur = self._db.execute(sql, params or ())
            self._db.commit()
            return [{k.lower(): r[k] for k in r.keys()} for r in cur.fetchall()]

    def insert_events(self, rows):
        with self._lock:
            self._db.executemany("INSERT INTO EVENTS (TS, KIND, DATA) VALUES (?,?,?)", rows)
            self._db.commit()

    def insert_sightings(self, rows):
        with self._lock:
            self._db.executemany("INSERT INTO SIGHTINGS (TS, LABEL, X, Y, W, H, FRAME_PATH) "
                                 "VALUES (?,?,?,?,?,?,?)", rows)
            self._db.commit()

    def complete(self, prompt, temperature=None):
        return ollama(prompt, temperature=0.2 if temperature is None else temperature)

    def search(self, question, domain=None, k=4):
        words = set(re.findall(r"[a-z]{3,}", question.lower()))
        docs = list(_OFFLINE_DOCS)
        for f in [*(ROOT / "data" / "docs").glob("*.txt"), *(ROOT / "data" / "docs").glob("*.md")]:
            d = f.name.split("__")[0] if "__" in f.name else "general"
            docs += [(d, f.stem, p) for p in f.read_text().split("\n\n") if p.strip()]
        scored = []
        for d, title, chunk in docs:
            if domain and d != domain:
                continue
            s = len(words & set(re.findall(r"[a-z]{3,}", (title + " " + chunk).lower())))
            if s:
                scored.append((s, {"chunk": chunk, "title": title, "domain": d}))
        return [h for _, h in sorted(scored, key=lambda t: -t[0])[:k]]

    def upload_frame(self, path):
        pass

    def run_readonly(self, sql):
        ro = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        ro.row_factory = sqlite3.Row
        try:
            return [{k.lower(): r[k] for k in r.keys()} for r in ro.execute(sql).fetchall()]
        finally:
            ro.close()

    def analyst_sql(self, question, timeout=15):
        return None


# ---------------------------------------------------------------- module state
_backend = None
_mock_forced = os.getenv("TEDDY_MOCK") == "1"
_q = queue.Queue()
_feed = deque(maxlen=300)
_feed_id = 0
_feed_lock = threading.Lock()
_listeners = []
_seen = {}  # label -> dict(ts, x, y, w, h, frame_path, t), instant memory for last_seen
_frame_source = None  # callable -> BGR numpy frame (set by the agent from vision.frame)
_last_frame = {"t": 0, "path": None}
_last_upload = [0.0]


def configure(mock=None):
    """Pick the backend. mock=None -> Snowflake if creds exist, else local."""
    global _backend, _mock_forced
    if mock is not None:
        _mock_forced = mock
    have_creds = all(os.getenv(k) for k in ("SNOWFLAKE_ACCOUNT", "SNOWFLAKE_USER", "SNOWFLAKE_PASSWORD"))
    if _mock_forced or not have_creds:
        if not _mock_forced:
            print("[brain] No Snowflake creds in .env -> using local mock memory")
        _backend = LocalBackend()
    else:
        _backend = SnowflakeBackend()
    return _backend


def backend():
    if _backend is None:
        configure()
    return _backend


def _writer():
    while True:
        fn, rows = _q.get()
        try:
            fn(rows)
        except Exception as e:
            print(f"[brain] write failed: {e}")


threading.Thread(target=_writer, daemon=True, name="teddy-sf-writer").start()


def on_event(fn):
    """Register a callback(event_dict) for every logged event (web live feed)."""
    _listeners.append(fn)


def _push_feed(kind, data, ts):
    global _feed_id
    with _feed_lock:
        _feed_id += 1
        ev = {"id": _feed_id, "ts": ts, "kind": kind, "data": data}
        _feed.append(ev)
    for fn in list(_listeners):
        try:
            fn(ev)
        except Exception:
            pass
    return ev


# ---------------------------------------------------------------- contract API
def log_event(kind, data=None):
    """Non-blocking: shows in the live feed now, lands in Snowflake a moment later."""
    data = data or {}
    ts = _ts()
    _push_feed(kind, data, ts)
    _q.put((backend().insert_events, [(ts, kind, json.dumps(data, default=str))]))


def set_frame_source(fn):
    """Give the brain the camera (vision.frame) so sightings carry a photo."""
    global _frame_source
    _frame_source = fn


def save_frame(img=None, tag="frame", upload=False, box=None):
    """Save a BGR frame (faces pixelated, optionally cropped to `box`) to data/frames and the @FRAMES stage."""
    if img is None and _frame_source:
        try:
            img = _frame_source()
        except Exception:
            img = None
    if img is None:
        return None
    try:
        import cv2
        from brain import privacy
        img = privacy.safe(img, box)
        FRAMES_DIR.mkdir(parents=True, exist_ok=True)
        name = f"{dt.datetime.now():%Y%m%d_%H%M%S}_{re.sub(r'[^a-z0-9]+', '-', tag.lower())[:24]}.jpg"
        cv2.imwrite(str(FRAMES_DIR / name), img, [cv2.IMWRITE_JPEG_QUALITY, 80])
    except Exception as e:
        print(f"[brain] save_frame failed: {e}")
        return None
    if upload or time.time() - _last_upload[0] > 30:  # keep stage uploads light
        _last_upload[0] = time.time()
        _q.put((backend().upload_frame, FRAMES_DIR / name))
    return name


def log_sighting(label, x, y, frame_path=None, w=None, h=None):
    """Called by the vision loop constantly; only stores when something is new or moved.
    Grabs one camera frame per ~10 s (shared by every label seen in it) for the photo memory."""
    label = (label or "").lower().strip()
    if not label:
        return
    now = time.time()
    prev = _seen.get(label)
    if prev and not frame_path and now - prev["t"] < 5 and abs(prev["x"] - x) < 0.05 and abs(prev["y"] - y) < 0.05:
        return
    if not frame_path:
        from brain import privacy
        if privacy.SIGHTING_FRAMES and now - _last_frame["t"] > 10:  # off by default: room shots can show the kid
            _last_frame.update(t=now, path=save_frame(tag="seen"))
        frame_path = _last_frame["path"] if privacy.SIGHTING_FRAMES else None
    ts = _ts()
    _seen[label] = {"ts": ts, "x": float(x), "y": float(y), "w": w, "h": h, "frame_path": frame_path, "t": now}
    _q.put((backend().insert_sightings, [(ts, label, float(x), float(y), w, h, frame_path)]))


def nearby(label, ts, x, y, window_s=15):
    """Closest other object seen around the same moment -> label or None ("by the laptop")."""
    t = dt.datetime.fromisoformat(str(ts)[:19])
    lo, hi = _ts(t - dt.timedelta(seconds=window_s)), _ts(t + dt.timedelta(seconds=window_s))
    try:
        rows = backend().query(
            "SELECT LABEL, AVG(X) AS X, AVG(Y) AS Y FROM SIGHTINGS WHERE TS BETWEEN %s AND %s "
            "AND LOWER(LABEL) <> %s AND LABEL <> 'person' GROUP BY LABEL", (lo, hi, label))
    except Exception as e:
        print(f"[brain] nearby failed: {e}")
        return None
    rows = [r for r in rows if r["x"] is not None]
    if not rows:
        return None
    best = min(rows, key=lambda r: (float(r["x"]) - x) ** 2 + (float(r["y"]) - y) ** 2)
    return best["label"] if ((float(best["x"]) - x) ** 2 + (float(best["y"]) - y) ** 2) ** .5 < 0.35 else None


def last_seen(label):
    """-> {"label","ts","x","y","w","h","frame_path","where","when","near"} or None."""
    label = (label or "").lower().strip()
    hit = _seen.get(label)
    if hit:
        row = dict(hit)
    else:
        try:
            rows = backend().query("SELECT TS, LABEL, X, Y, W, H, FRAME_PATH FROM SIGHTINGS "
                                   "WHERE LOWER(LABEL) LIKE %s ORDER BY TS DESC LIMIT 1", (f"%{label}%",))
        except Exception as e:
            print(f"[brain] last_seen failed: {e}")
            rows = []
        if not rows:
            return None
        row = rows[0]
        row["ts"] = str(row["ts"])[:19]
    x, y = float(row["x"]), float(row["y"])
    return {"label": label, "ts": row["ts"], "x": x, "y": y, "w": row.get("w"), "h": row.get("h"),
            "frame_path": row.get("frame_path"), "where": describe_spot(x, y), "when": describe_time(row["ts"]),
            "near": nearby(label, row["ts"], x, y)}


def describe_spot(x, y):
    h = "on the left" if x < 0.35 else "on the right" if x > 0.65 else "in the middle"
    v = "up high" if y < 0.35 else "down low" if y > 0.65 else ""
    return f"{h} {v}".strip()


def describe_time(ts):
    t = dt.datetime.fromisoformat(str(ts)[:19])
    mins = (_now() - t).total_seconds() / 60
    if mins < 2:
        return "just now"
    if mins < 60:
        return f"{int(mins)} minutes ago"
    day = "today" if t.date() == _now().date() else "yesterday" if (_now().date() - t.date()).days == 1 \
        else t.strftime("on %A")
    return f"{day} at {t.strftime('%-I:%M %p')}"


def complete(prompt, temperature=None):
    return backend().complete(prompt, temperature)


def ask_detailed(question, domain=None):
    """RAG: Cortex Search retrieval + AI_COMPLETE. Answers only from sources and names the source."""
    t0 = time.time()
    b = backend()
    try:
        hits = b.search(question, domain) or (b.search(question) if domain else [])
    except Exception as e:
        print(f"[brain] search failed: {e}")
        hits = []
    source = url = None
    if not hits:
        ans = NOT_SURE
    else:
        sources = "\n\n".join(f"SOURCE {i + 1} ({h.get('title', '')}):\n{h['chunk']}" for i, h in enumerate(hits))
        # Question first: with it up top llama3.3-70b pulls the concrete steps instead of echoing
        # whichever chunk ranked first (tested on choking / CPR rate / burns).
        prompt = (
            f"QUESTION: {question}\n\n"
            "You are Teddy, a gentle teddy bear talking out loud to a child or an older adult. Using ONLY these "
            "sources, answer the question above in at most 3 short, simple spoken sentences with the specific "
            "actions and numbers. No lists or markdown. Start with 'Call 911' only if someone may be in danger right now. "
            f"If the sources don't answer it, reply exactly: \"{NOT_SURE}\"\n"
            "End with a line: SOURCE: <number of the source you used most>\n\n"
            f"{sources}\n\nTEDDY SAYS:")
        try:
            raw = b.complete(prompt, temperature=0)
            m = re.search(r"(?:^|\n)\s*\**SOURCE\**:?\s*\**(\d+)\**\s*$", raw.strip(), re.I)
            n = int(m.group(1)) if m else 1
            ans = _spoken(raw.strip()[:m.start()] if m else raw) or NOT_SURE
            if NOT_SURE.split(".")[0].lower() in ans.lower() or n == 0:
                ans = NOT_SURE
            else:
                hit = hits[min(max(n, 1), len(hits)) - 1]
                source, url = hit.get("title"), hit.get("source_url")
        except Exception as e:
            print(f"[brain] complete failed: {e}")
            ans = NOT_SURE
    spoken = f"{ans} That's from the {source}." if source else ans
    out = {"answer": spoken, "text": ans, "source": source, "url": url, "sources": [h.get("title") for h in hits],
           "domain": domain, "engine": b.name, "model": getattr(b, "model", None) or OLLAMA_MODEL,
           "ms": int((time.time() - t0) * 1000)}
    log_event("ask", {"text": question, "domain": domain, "reply": spoken, "source": source, "url": url})
    return out


def ask(question, domain=None):
    return ask_detailed(question, domain)["answer"]


# ---------------------------------------------------------------- caregiver analytics
_SCHEMA_DOC = """Tables:
EVENTS_FLAT(TS timestamp, DAY date, KIND text, SOURCE text, INTENT text, OBJECT text, MOOD_SCORE int 1-5,
  MOOD text, STATUS text, HEART_RATE float, TEXT text, REPLY text)
  KIND values: intent, gesture, mood, fall_check, alert, speak, heard, vitals, ask, and for kids: howto_start,
  howto_step, skill_done, homework_start, homework_try, homework_done, find, read, badge, mood_checkin, upset_moment.
  Kid columns: SKILL, FEELING, HELPING_WITH (what Teddy was helping with), DURATION_S, BADGE, QUESTION, CORRECT.
MOODS(TS timestamp, KID text, LABEL text calm|happy|sad|frustrated|upset, CONF float, ACTIVITY text)
  = mood check-in readings (labels only, not a diagnosis).
  mood rows have MOOD_SCORE and MOOD. alert rows have STATUS (fallen, no_response, help_requested).
  heard rows have TEXT = what the person said. vitals rows have HEART_RATE.
SIGHTINGS(TS timestamp, LABEL text, X float 0-1 left-right, Y float 0-1 top-bottom)
  = objects the bear's camera saw (keys, glasses, phone, remote, pill bottle, wallet...).
There is only one person, so never filter by name.

Examples:
Q: where did I leave my keys?
SQL: SELECT LABEL, TS, X, Y FROM SIGHTINGS WHERE LOWER(LABEL) LIKE '%key%' ORDER BY TS DESC LIMIT 3
Q: how was she this week?
SQL: SELECT TS, KIND, MOOD, MOOD_SCORE, STATUS, TEXT FROM EVENTS_FLAT WHERE KIND IN ('mood','alert') AND TS >= {week_ago} ORDER BY TS
Q: did she fall recently?
SQL: SELECT TS, KIND, STATUS, TEXT FROM EVENTS_FLAT WHERE KIND IN ('alert','fall_check') AND STATUS <> 'ok' ORDER BY TS DESC LIMIT 10
Q: what has she been asking for?
SQL: SELECT INTENT, COUNT(*) AS N FROM EVENTS_FLAT WHERE KIND = 'intent' AND TS >= {week_ago} GROUP BY INTENT ORDER BY N DESC"""

_BAD_SQL = re.compile(r"\b(insert|update|delete|merge|drop|create|alter|grant|truncate|call|put|copy)\b", re.I)


def _clean_sql(sql):
    sql = re.sub(r"```(sql)?", "", sql or "", flags=re.I).strip().rstrip(";")
    m = re.search(r"\b(with|select)\b.*", sql, re.I | re.S)
    sql = m.group(0) if m else ""
    if not sql or ";" in sql or _BAD_SQL.search(sql):
        return None
    return sql


def caregiver_ask(question):
    """Caregiver question -> SQL (Cortex Analyst, else COMPLETE text-to-SQL) -> rows -> warm summary."""
    t0 = time.time()
    b = backend()
    sql, engine = None, None
    if b.name == "snowflake" and os.getenv("TEDDY_ANALYST", "1") == "1":
        try:
            sql, engine = _clean_sql(b.analyst_sql(question)), "cortex_analyst"
        except Exception as e:
            print(f"[brain] analyst unavailable ({e}); using COMPLETE text-to-SQL")
    if not sql:
        engine = f"{b.name}_text_to_sql"
        prompt = (f"Write ONE read-only {b.dialect} query that answers the question. "
                  f"Now is {_ts()}.\n{_SCHEMA_DOC.format(week_ago=b.week_ago)}\n"
                  "Return only the SQL, no explanation. LIMIT 50 rows.\n"
                  f"QUESTION: {question}\nSQL:")
        try:
            sql = _clean_sql(b.complete(prompt))
        except Exception as e:
            print(f"[brain] text-to-sql failed: {e}")
    rows, err = [], None
    if sql:
        try:
            rows = b.run_readonly(sql)[:50]
        except Exception as e:
            err = str(e)
    rows = [{k: (str(v) if isinstance(v, (dt.datetime, dt.date)) else v) for k, v in r.items()} for r in rows]
    for r in rows:  # pre-chew times/positions so the LLM doesn't do date math
        if r.get("ts"):
            try:
                r["when"] = describe_time(r["ts"])
            except ValueError:
                pass
        if r.get("x") is not None and r.get("y") is not None:
            r["spot"] = describe_spot(float(r["x"]), float(r["y"]))
    try:
        st = stats()
        digest = {"mood_by_day": dict(zip(st["days"], st["mood"])),
                  "alerts": [{**a, "when": describe_time(a["ts"])} for a in st["alerts"][:5]],
                  "objects_last_seen": st["last_seen"], "top_requests": st["intents"]}
    except Exception:
        digest = {}
    summary_prompt = (
        f"You help a parent/caregiver check on {OWNER}, who uses Teddy, an AI teddy bear buddy. "
        f"Now is {_now():%A %B %-d, %-I:%M %p}.\n"
        f"Caregiver asked: {question}\nData from the bear's log (JSON rows):\n"
        f"{json.dumps(rows[:40], default=str)}\n"
        f"Weekly digest (mood 1-5 per day, today last): {json.dumps(digest, default=str)}\n"
        "Answer in 1-3 warm, plain sentences using only this data. Feelings are mood check-ins, never a "
        "diagnosis. Prefer the query rows; use the digest "
        "for context. Use the 'when' and 'spot' fields as written. Mention times in a friendly way "
        "(e.g. 'Tuesday afternoon'). If the data is empty, say the bear hasn't noticed anything about that yet.")
    try:
        answer = _spoken(b.complete(summary_prompt))
    except Exception as e:
        answer = f"Sorry, I couldn't reach the bear's memory ({e})."
    out = {"answer": answer, "sql": sql, "rows": rows[:20], "engine": engine, "error": err,
           "ms": int((time.time() - t0) * 1000)}
    log_event("caregiver_ask", {"text": question, "reply": answer, "engine": engine})
    return out


def recent_events(after=0, limit=50):
    with _feed_lock:
        evs = [e for e in _feed if e["id"] > after]
    return evs[-limit:]


def stats(days=7):
    """Aggregates for the caregiver dashboard (done in Python so both backends match)."""
    since = _ts(_now() - dt.timedelta(days=days))
    b = backend()
    ev = b.query("SELECT TS, KIND, DATA FROM EVENTS WHERE TS >= %s ORDER BY TS", (since,))
    sg = b.query("SELECT LABEL, MAX(TS) AS TS FROM SIGHTINGS GROUP BY LABEL ORDER BY 2 DESC LIMIT 12")
    day_keys = [(_now() - dt.timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days - 1, -1, -1)]
    mood = {d: [] for d in day_keys}
    talks = {d: 0 for d in day_keys}
    hours = [0] * 24
    alerts, intents = [], {}
    for r in ev:
        ts = str(r["ts"])[:19]
        d = ts[:10]
        data = r["data"] if isinstance(r["data"], dict) else json.loads(r["data"] or "{}")
        kind = r["kind"]
        if kind == "mood" and d in mood and data.get("score"):
            mood[d].append(float(data["score"]))
        if kind in ("heard", "intent", "gesture") and d in talks:
            talks[d] += 1
            hours[int(ts[11:13])] += 1
        if kind == "intent":
            intents[data.get("intent", "?")] = intents.get(data.get("intent", "?"), 0) + 1
        if kind == "alert":
            alerts.append({"ts": ts, **data})
    last_mood = next((json.loads(r["data"]) if isinstance(r["data"], str) else r["data"]
                      for r in reversed(ev) if r["kind"] == "mood"), None)
    return {
        "owner": OWNER, "engine": b.name,
        "days": [dt.date.fromisoformat(d).strftime("%a") for d in day_keys],
        "mood": [round(sum(v) / len(v), 2) if v else None for v in mood.values()],
        "interactions": list(talks.values()),
        "hours": hours,
        "intents": dict(sorted(intents.items(), key=lambda kv: -kv[1])),
        "alerts": alerts[-10:][::-1],
        "last_mood": last_mood,
        "last_seen": [{"label": r["label"], "when": describe_time(r["ts"]), "ts": str(r["ts"])[:19]} for r in sg],
    }
