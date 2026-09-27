"""How-to coach knowledge base: step-by-step guides for kids, stored in Snowflake, found with Cortex Search.

    python -m brain.howto --load     # brain/guides/guides.json -> HOWTO_GUIDES / HOWTO_STEPS + HOWTO_SEARCH
    python -m brain.howto "how do I tie my shoes"

Snowflake: HOWTO_GUIDES (one row per guide), HOWTO_STEPS (one row per step) and the Cortex Search
service HOWTO_SEARCH over title + summary + keywords + step text. Offline, the same JSON is searched locally.
"""
import json
import re
import sys
from functools import lru_cache
from pathlib import Path

from brain import snowflake as sf

GUIDES_FILE = Path(__file__).parent / "guides" / "guides.json"
SEARCH_SERVICE = "TEDDY.CORE.HOWTO_SEARCH"


@lru_cache(maxsize=1)
def _local():
    return {g["id"]: g for g in json.loads(GUIDES_FILE.read_text())}


def _words(text):
    return set(re.findall(r"[a-z]{3,}", (text or "").lower())) - {"how", "the", "can", "you", "and", "show", "teach",
                                                                   "help", "want", "learn", "please", "teddy", "what"}


def load_to_snowflake(guides=None):
    """(Re)load the guides and rebuild the HOWTO_SEARCH service."""
    b = sf.backend()
    guides = guides or list(_local().values())
    b.query("CREATE TABLE IF NOT EXISTS HOWTO_GUIDES (GUIDE_ID STRING, TITLE STRING, SKILL STRING, BADGE STRING, "
            "AUDIENCE STRING, ICON STRING, SUMMARY STRING, KEYWORDS STRING, SOURCE STRING, SEARCH_TEXT STRING)")
    b.query("CREATE TABLE IF NOT EXISTS HOWTO_STEPS (GUIDE_ID STRING, STEP_NO INT, SAY STRING, SHOW STRING, "
            "TIP STRING, DRAW STRING)")
    ids = [g["id"] for g in guides]
    marks = ",".join(["%s"] * len(ids))
    b.query(f"DELETE FROM HOWTO_STEPS WHERE GUIDE_ID IN ({marks})", ids)
    b.query(f"DELETE FROM HOWTO_GUIDES WHERE GUIDE_ID IN ({marks})", ids)
    for g in guides:
        text = " ".join([g["title"], g.get("summary", ""), g.get("keywords", "")] + [s["show"] for s in g["steps"]])
        b.query("INSERT INTO HOWTO_GUIDES VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (g["id"], g["title"], g["skill"], g["badge"], g.get("audience", "kid"), g.get("icon", ""),
                 g.get("summary", ""), g.get("keywords", ""), g.get("source", "Teddy guides"), text))
        rows = [v for i, s in enumerate(g["steps"], 1)
                for v in (g["id"], i, s["say"], s["show"], s.get("tip", ""), s.get("draw", ""))]
        b.query("INSERT INTO HOWTO_STEPS VALUES " + ",".join(["(%s,%s,%s,%s,%s,%s)"] * len(g["steps"])), rows)
    if b.name == "snowflake":
        b.query(f"""CREATE OR REPLACE CORTEX SEARCH SERVICE {SEARCH_SERVICE}
                    ON SEARCH_TEXT ATTRIBUTES AUDIENCE, GUIDE_ID
                    WAREHOUSE = {sf.WAREHOUSE} TARGET_LAG = '1 hour'
                    AS SELECT SEARCH_TEXT, AUDIENCE, GUIDE_ID, TITLE FROM HOWTO_GUIDES""", timeout=300)
    return len(guides)


def get_guide(guide_id):
    """-> {"id","title","skill","badge","icon","steps":[{"say","show","tip","draw"}]} or None."""
    b = sf.backend()
    if b.name == "snowflake":
        try:
            g = b.query("SELECT GUIDE_ID, TITLE, SKILL, BADGE, ICON, SOURCE FROM HOWTO_GUIDES WHERE GUIDE_ID = %s",
                        (guide_id,), timeout=15)
            if g:
                steps = b.query("SELECT SAY, SHOW, TIP, DRAW FROM HOWTO_STEPS WHERE GUIDE_ID = %s ORDER BY STEP_NO",
                                (guide_id,), timeout=15)
                g = g[0]
                return {"id": g["guide_id"], "title": g["title"], "skill": g["skill"], "badge": g["badge"],
                        "icon": g["icon"], "source": g["source"], "engine": "snowflake",
                        "steps": [{k: r[k] or "" for k in ("say", "show", "tip", "draw")} for r in steps]}
        except Exception as e:
            print(f"[howto] snowflake read failed, using local guides: {e}")
    g = _local().get(guide_id)
    return {**g, "engine": "local"} if g else None


def find_guide(query, audience="kid"):
    """Cortex Search over the guides. Only returns a guide that really matches (no made-up steps)."""
    words = _words(query)
    b = sf.backend()
    candidates = []
    if b.name == "snowflake":
        try:
            req = {"query": query, "columns": ["GUIDE_ID", "TITLE", "SEARCH_TEXT"], "limit": 3,
                   "filter": {"@eq": {"AUDIENCE": audience}}}
            r = b.query("SELECT SNOWFLAKE.CORTEX.SEARCH_PREVIEW(%s, %s) AS R", (SEARCH_SERVICE, json.dumps(req)),
                        timeout=15)
            candidates = [(h["GUIDE_ID"], h.get("SEARCH_TEXT", "")) for h in json.loads(r[0]["r"]).get("results", [])]
        except Exception as e:
            print(f"[howto] search failed, using local match: {e}")
    if not candidates:
        scored = sorted(((len(words & _words(g["title"] + " " + g["keywords"])), gid)
                         for gid, g in _local().items() if g.get("audience", "kid") == audience), reverse=True)
        candidates = [(gid, _local()[gid]["title"] + " " + _local()[gid]["keywords"]) for n, gid in scored if n]
    for gid, text in candidates:  # the top hit must share a real word with the question
        if words & _words(text):
            return get_guide(gid)
    return None


def menu(audience="kid"):
    return [{"id": g["id"], "title": g["title"], "icon": g.get("icon")}
            for g in _local().values() if g.get("audience", "kid") == audience]


_NEXT = r"\b(done|did it|next|finished|got it|ok|okay|yes|yep|yeah|ready|all done|i did|now what|what's next)\b"
_REPEAT = r"\b(again|repeat|what did you say|say it again|one more time|huh|pardon)\b|^what\??$"
_HELP = r"\b(help|stuck|hard|can't|cannot|don't get it|dont get it|how|confused|hint|tricky)\b"
_BACK = r"\b(back|go back|previous|last step|before)\b"
_STOP = r"\b(stop|quit|no more|enough|done with this|bye|cancel)\b"


def classify_reply(text):
    """Kid's answer during a how-to step -> next | repeat | help | back | stop | None."""
    t = (text or "").lower().strip()
    if not t:
        return None
    for kind, pat in (("stop", _STOP), ("back", _BACK), ("repeat", _REPEAT), ("help", _HELP), ("next", _NEXT)):
        if re.search(pat, t):
            return kind
    return None


if __name__ == "__main__":
    sf.configure()
    if "--load" in sys.argv:
        print(f"Loaded {load_to_snowflake()} guides into {sf.backend().name} (+ HOWTO_SEARCH)")
    else:
        q = " ".join(a for a in sys.argv[1:] if not a.startswith("-")) or "how do I tie my shoes"
        g = find_guide(q)
        print(q, "->", g and (g["id"], g["engine"], len(g["steps"]), "steps"))
