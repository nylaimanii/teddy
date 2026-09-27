"""Immersive CPR mode: full-screen step cards, arms pumping at 110/min, a big spoken count.

The cards come ONLY from the AHA guidelines in Snowflake (DOC_SEARCH, domain cpr) via AI_COMPLETE,
built once and cached as the hidden how-to guide "cpr" (audience = emergency). Card one is always
"get a grown-up and call 911". No button starts this: only a voice request does.

    python -m brain.cpr --build     # (re)build the cards from the AHA doc
"""
import json
import re
import sys

from brain import howto
from brain import snowflake as sf

FIRST = {"show": "Get a grown-up. Call 911.",
         "say": "First, shout for a grown-up and call 911. Put the phone on speaker."}
SOURCE = "AHA 2025 CPR Guidelines Highlights"


_NUM = re.compile(r"\d+(?:[.,]\d+)?")
_FACT_WORDS = ("inch", "centimet", " cm", "per minute", "a minute", "/min", "seconds")


def grounded(card, source_text):
    """A card may only state numbers (depths, rates, counts) that appear in the AHA text itself."""
    text = f"{card['show']} {card['say']}".lower()
    for n in _NUM.findall(text):
        if not re.search(rf"(?<![\d.]){re.escape(n)}(?![\d])", source_text):
            return False
    for w in _FACT_WORDS:  # units the model likes to add from memory
        if w in text and w not in source_text:
            return False
    return True


def build():
    """AHA chunks -> 3-5 short step cards (only what the doc says) -> HOWTO tables as guide 'cpr'."""
    b = sf.backend()
    hits = []
    for q in ("how to give chest compressions adult CPR steps hand position depth rate",
              "hands-only CPR bystander push hard and fast", "CPR sequence compressions breaths AED"):
        try:
            hits += b.search(q, "cpr", k=4)
        except Exception as e:
            print(f"[cpr] search failed: {e}")
    seen, chunks = set(), []
    for h in hits:
        if h["chunk"] not in seen:
            seen.add(h["chunk"]); chunks.append(h["chunk"])
    if not chunks:
        return None
    prompt = (
        "From ONLY these American Heart Association guideline excerpts, write 3 to 5 very short CPR step cards "
        "for a scared bystander, in order, after they have already called 911. Use only facts stated in the "
        "excerpts; if the excerpts don't say something (like an exact depth or rate), leave it out. Simple words.\n"
        "Return ONLY JSON: {\"cards\": [{\"show\": \"a plain short phrase, max 6 words, like Push on the chest\", \"say\": \"one short spoken sentence\"}]}\n\n"
        + "\n\n---\n\n".join(chunks[:8]))
    raw = sf.complete(prompt, temperature=0)
    cards = json.loads(re.search(r"\{.*\}", raw, re.S).group(0))["cards"][:5]
    cards = [{"show": sf._spoken(c["show"]), "say": sf._spoken(c["say"]), "tip": "", "draw": ""} for c in cards if c.get("say")]
    source_text = " ".join(chunks).lower()
    kept = [c for c in cards if grounded(c, source_text)]
    for c in cards:
        if c not in kept:
            print(f"[cpr] dropped a card not supported by the AHA text: {c['say']!r}")
    cards = kept
    guide = {"id": "cpr", "title": "CPR", "skill": "CPR", "badge": "", "icon": "heart", "audience": "emergency",
             "summary": "CPR steps from the AHA guidelines", "keywords": "cpr chest compressions not breathing",
             "source": SOURCE, "steps": [dict(FIRST, tip="", draw="")] + cards}
    howto.load_to_snowflake([guide])
    return guide


def cards():
    """The cards checked at build time (python -m brain.cpr --build). Never generated mid-emergency:
    without them, CPR mode shows only 'get a grown-up, call 911' and keeps the count."""
    try:
        g = howto.get_guide("cpr")
        if g and g.get("steps"):
            return g["steps"], g.get("source") or SOURCE
    except Exception as e:
        print(f"[cpr] couldn't read cards: {e}")
    return [FIRST], None


if __name__ == "__main__":
    sf.configure()
    if "--build" in sys.argv:
        g = build()
        for i, c in enumerate(g["steps"] if g else []):
            print(f"  {i + 1}. {c['show']:34} | {c['say']}")
    else:
        steps, src = cards()
        print(src, [c["show"] for c in steps])
