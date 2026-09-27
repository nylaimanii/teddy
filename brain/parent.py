"""Everything the Parent page shows. Runs anywhere (Mac or Vultr): it only needs Snowflake (+ Tiger Data).

    week()            practice per day, skills learned + badges, homework, finds, feelings, upset moments
    insight()         a short weekly note written by Snowflake Cortex (AI_COMPLETE) from the week's numbers
    ask(question)     "where are the keys?" -> last seen (+ photo); anything else -> Cortex Analyst / text-to-SQL

Charts read Tiger Data (practice_daily / moods_daily continuous aggregates) when it's configured, and fall
back to the same numbers from Snowflake EVENTS / MOODS otherwise. Moods are check-ins, never a diagnosis.
"""
import datetime as dt
import json
import re
from collections import Counter, defaultdict

from brain import flags
from brain import snowflake as sf

FEELINGS = ["calm", "happy", "sad", "frustrated", "upset"]


def _days(n):
    today = dt.date.today()
    return [today - dt.timedelta(days=i) for i in range(n - 1, -1, -1)]


def _events(kinds, days):
    since = sf._ts(dt.datetime.now() - dt.timedelta(days=days))
    marks = ",".join(["%s"] * len(kinds))
    rows = sf.backend().query(f"SELECT TS, KIND, DATA FROM EVENTS WHERE TS >= %s AND KIND IN ({marks}) ORDER BY TS",
                              [since, *kinds])
    for r in rows:
        r["data"] = r["data"] if isinstance(r["data"], dict) else json.loads(r["data"] or "{}")
        r["ts"] = str(r["ts"])[:19]
    return rows


def _practice(days):
    """-> {date: {skill: minutes}}, sessions per skill. Tiger first, Snowflake fallback."""
    per_day, sessions = defaultdict(lambda: defaultdict(float)), Counter()
    if flags.tiger():
        try:
            from brain import tiger
            for r in tiger.practice_by_day(days):
                per_day[r["day"]][r["skill"]] += float(r["minutes"] or 0)
                sessions[r["skill"]] += int(r["sessions"] or 0)
            return per_day, sessions, "tiger"
        except Exception as e:
            print(f"[parent] tiger practice failed, using Snowflake: {e}")
    for r in _events(["howto_start", "homework_start", "skill_done", "homework_done"], days):
        d, skill = dt.date.fromisoformat(r["ts"][:10]), r["data"].get("skill")
        if not skill:
            continue
        if r["kind"] in ("howto_start", "homework_start"):
            sessions[skill] += 1
        else:
            per_day[d][skill] += (r["data"].get("duration_s") or 0) / 60
    return per_day, sessions, sf.backend().name


def _feelings(days):
    counts = defaultdict(Counter)
    if flags.tiger():
        try:
            from brain import tiger
            for r in tiger.moods_by_day(days):
                counts[r["day"]][r["label"]] += int(r["readings"])
            return counts, "tiger"
        except Exception as e:
            print(f"[parent] tiger moods failed, using Snowflake: {e}")
    since = sf._ts(dt.datetime.now() - dt.timedelta(days=days))
    try:
        rows = sf.backend().query("SELECT TO_DATE(TS) AS D, LABEL, COUNT(*) AS N FROM MOODS WHERE TS >= %s GROUP BY 1, 2"
                                  if sf.backend().name == "snowflake" else
                                  "SELECT date(TS) AS D, LABEL, COUNT(*) AS N FROM MOODS WHERE TS >= %s GROUP BY 1, 2",
                                  (since,))
    except Exception:
        rows = []
    for r in rows:
        counts[dt.date.fromisoformat(str(r["d"])[:10])][r["label"]] += int(r["n"])
    return counts, sf.backend().name


def week(days=7):
    ds = _days(days)
    per_day, sessions, practice_src = _practice(days)
    feel, feel_src = _feelings(days)
    done = _events(["skill_done", "badge"], days)
    badges = {r["data"].get("badge"): r["data"] for r in done if r["kind"] == "badge"}
    skills = []
    for r in done:
        if r["kind"] == "skill_done":
            b = badges.get(r["data"].get("badge"), {})
            skills.append({"skill": r["data"].get("skill"), "badge": r["data"].get("badge"), "when": r["ts"],
                           "minutes": round((r["data"].get("duration_s") or 0) / 60, 1),
                           "solana": b.get("url"), "mint": b.get("mint")})
    hw = _events(["homework_start", "homework_done", "homework_try"], days)
    problems = {}
    for r in hw:
        q = r["data"].get("question")
        if not q:
            continue
        p = problems.setdefault(q, {"question": q, "when": r["ts"], "tries": 0, "done": False})
        p["tries"] += r["kind"] == "homework_try"
        p["done"] |= r["kind"] == "homework_done"
    finds = [r["data"].get("object") for r in _events(["find"], days) if r["data"].get("object")]
    upsets = [{"when": r["data"].get("started") or r["ts"], "feeling": r["data"].get("feeling"),
               "seconds": r["data"].get("duration_s"), "helping_with": r["data"].get("helping_with"),
               "checked_in": r["data"].get("checked_in")}
              for r in _events(["upset_moment"], days)][::-1]
    return {
        "kid": flags.KID_NAME,
        "days": [d.isoformat() for d in ds],
        "labels": [d.strftime("%a") for d in ds],
        "practice_minutes": [round(sum(per_day.get(d, {}).values()), 1) for d in ds],
        "practice_by_skill": {k: v for k, v in sessions.most_common()},
        "skills": skills[::-1],
        "homework": sorted(problems.values(), key=lambda p: p["when"], reverse=True)[:10],
        "finds": dict(Counter(finds).most_common()),
        "feelings": {f: [feel.get(d, {}).get(f, 0) for d in ds] for f in FEELINGS},
        "upsets": upsets[:12],
        "sources": {"practice": practice_src, "feelings": feel_src, "brain": sf.backend().name,
                    "badges": "solana devnet" if flags.solana() else "off"},
    }


def insight(w=None):
    """A warm 2-3 sentence note for the parent, written by Cortex from the week's numbers only."""
    w = w or week()
    facts = {"skills_finished": [s["skill"] for s in w["skills"]], "practice_sessions": w["practice_by_skill"],
             "minutes_per_day": dict(zip(w["labels"], w["practice_minutes"])),
             "homework": [{"q": h["question"], "tries": h["tries"], "solved": h["done"]} for h in w["homework"]],
             "things_found": w["finds"],
             "feelings_totals": {f: sum(v) for f, v in w["feelings"].items()},
             "upset_moments": [{"when": u["when"], "feeling": u["feeling"], "while": u["helping_with"]} for u in w["upsets"]]}
    prompt = (f"You write a short weekly note to a parent about their kid {w['kid']} (age 5-10), who uses Teddy, a teddy "
              "bear buddy that helps kids do things on their own. Use ONLY these facts:\n"
              f"{json.dumps(facts, default=str)}\n"
              "Write 2-3 warm, plain sentences: what they practiced and learned, and one gentle observation about the "
              "mood check-ins (these are check-ins, not a diagnosis; never diagnose). No lists, no markdown.")
    try:
        return {"text": sf._spoken(sf.complete(prompt, temperature=0.3)), "by": "Snowflake Cortex AI_COMPLETE"}
    except Exception as e:
        return {"text": "", "by": None, "error": str(e)[:120]}


_WHERE = re.compile(r"where(?:'s| is| are| did (?:i|we|she|he|they) (?:leave|put))\s+(?:my|the|our|her|his|their)?\s*([a-z][a-z ]{1,24}?)\s*\??$", re.I)


def photo_url(frame_path, seconds=3600):
    """A signed link to the (face-blurred) photo in the Snowflake @FRAMES stage."""
    if not frame_path or sf.backend().name != "snowflake":
        return None
    try:
        return sf.backend().query("SELECT GET_PRESIGNED_URL(@TEDDY.CORE.FRAMES, %s, %s) AS U", (frame_path, seconds))[0]["u"]
    except Exception:
        return None


def ask(question):
    q = (question or "").strip()
    m = _WHERE.search(q)
    if m:
        obj = m.group(1).strip().lower()
        seen = sf.last_seen(obj)
        if seen:
            place = f"by the {seen['near']}" if seen.get("near") else seen["where"]
            return {"answer": f"Teddy last saw the {obj} {seen['when']}, {place}.", "engine": "snowflake SIGHTINGS",
                    "photo": photo_url(seen.get("frame_path")), "sql": None}
        return {"answer": f"Teddy hasn't seen the {obj} yet.", "engine": "snowflake SIGHTINGS", "sql": None}
    r = sf.caregiver_ask(q)
    return {"answer": r["answer"], "engine": r["engine"], "sql": r.get("sql")}
