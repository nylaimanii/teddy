"""Seed a realistic week of Teddy's memory so the caregiver dashboard has a story to tell.

    python -m brain.seed           # add 7 days of demo data (Snowflake if creds, else local)
    python -m brain.seed --reset   # wipe EVENTS/SIGHTINGS first

Story: Grandma Rose is doing fine early in the week, dips mid-week (lonely, knee pain, a fall
check on Wednesday night where she was OK), and perks up after her grandkid visits on Saturday.
"""
import datetime as dt
import json
import random
import sys

from brain import snowflake as sf

random.seed(7)

MOODS = {  # days ago -> (score, mood, what she said)
    6: (4, "happy", "I'm good, the garden looks lovely today"),
    5: (4, "okay", "Pretty good. Watched my shows"),
    4: (3, "tired", "A bit tired, didn't sleep well"),
    3: (2, "lonely", "It's quiet. Nobody called today"),
    2: (2, "in pain", "My knee is acting up again"),
    1: (5, "happy", "Wonderful! Maya came over and we baked cookies"),
    0: (4, "happy", "Good morning Teddy, I feel rested"),
}
SAYINGS = [
    "Teddy, where are my glasses?", "Where did I leave my keys?", "Can you read this letter for me?",
    "Tell me a joke", "What's this?", "What day is it?", "Did I take my pills?", "Let's dance!",
    "How do I treat a small burn?", "Good night Teddy", "Where's the remote?", "I miss Harold",
]
OBJECTS = {"keys": (0.78, 0.62), "glasses": (0.22, 0.40), "phone": (0.55, 0.70), "remote": (0.40, 0.75),
           "pill bottle": (0.85, 0.35), "wallet": (0.70, 0.60), "cup": (0.30, 0.66)}
# Furniture the camera always sees; gives "by the laptop" context to last_seen.
LANDMARKS = {"laptop": (0.74, 0.55), "couch": (0.20, 0.55), "tv": (0.47, 0.28), "kitchen table": (0.38, 0.80),
             "shelf": (0.86, 0.30)}


def at(days_ago, hour, minute=0):
    d = dt.datetime.now().replace(second=0, microsecond=0) - dt.timedelta(days=days_ago)
    return d.replace(hour=hour, minute=minute)


def build():
    ev, sg = [], []

    def sight(t, obj, x, y):
        sg.append((sf._ts(t), obj, round(x, 3), round(y, 3), 0.08, 0.08, None))
        for lm, (lx, ly) in LANDMARKS.items():
            sg.append((sf._ts(t), lm, lx, ly, 0.2, 0.2, None))

    def e(t, kind, **data):
        if t <= dt.datetime.now():
            ev.append((sf._ts(t), kind, json.dumps(data)))

    for d in range(6, -1, -1):
        score, mood, text = MOODS[d]
        busy = 3 if score <= 2 else 7
        # morning check-in
        t = at(d, 9, random.randint(0, 30))
        e(t, "gesture", type="wave", source="gesture")
        e(t, "intent", intent="mood_checkin", source="voice")
        e(t, "heard", text=text, source="voice")
        e(t, "mood", score=score, mood=mood, text=text)
        e(t, "vitals", heart_rate=random.randint(64, 78) + (8 if mood == "in pain" else 0),
          breathing_rate=random.randint(12, 16))
        # daytime chatter
        for _ in range(busy):
            t = at(d, random.randint(10, 20), random.randint(0, 59))
            said = random.choice(SAYINGS)
            e(t, "heard", text=said, source="voice")
            if "where" in said.lower():
                obj = next((o for o in OBJECTS if o.split()[0] in said.lower()), "keys")
                e(t, "intent", intent="find_object", object=obj, source="voice")
            elif "read" in said.lower():
                e(t, "intent", intent="read", source="voice")
            elif "dance" in said.lower():
                e(t, "intent", intent="dance", source=random.choice(["voice", "phone"]))
            elif "burn" in said.lower():
                e(t, "intent", intent="first_aid", source="voice")
            elif "this" in said.lower():
                e(t, "gesture", type="point", source="gesture")
                e(t, "intent", intent="identify", source="gesture")
            else:
                e(t, "intent", intent="chat", source="voice")
        # evening fall check (the bear watches when she's up late)
        e(at(d, 21, 15), "fall_check", status="ok")
        # objects moving around the house
        for obj, (x, y) in OBJECTS.items():
            for _ in range(random.randint(1, 3)):
                t = at(d, random.randint(8, 21), random.randint(0, 59))
                if t <= dt.datetime.now():
                    sight(t, obj, x + random.uniform(-.05, .05), y + random.uniform(-.05, .05))

    # the scary-but-OK moment on Wednesday night
    t = at(3, 22, 41)
    e(t, "fall_check", status="fallen")
    e(t, "alert", status="fallen", text="Teddy saw Rose on the floor by the couch")
    e(t, "speak", reply="Rose, are you okay? I'm right here.")
    e(t, "heard", text="I'm alright Teddy, I just slipped. My knee hurts.")
    e(t, "vitals", heart_rate=96, breathing_rate=20)
    e(t, "fall_check", status="ok", text="She said she was alright")
    # an unanswered check-in mid-week
    e(at(2, 15, 5), "alert", status="no_response", text="Rose didn't answer the afternoon check-in")
    e(at(2, 15, 25), "mood", score=2, mood="in pain", text="Sorry Teddy, I was lying down. Knee's sore.")
    # keys: last seen by the front door this morning
    sight(at(0, 8, 12) if at(0, 8, 12) <= dt.datetime.now() else at(1, 20, 5), "keys", 0.79, 0.58)
    return ev, sg


if __name__ == "__main__":
    b = sf.configure()
    if "--reset" in sys.argv:
        b.query("DELETE FROM EVENTS")
        b.query("DELETE FROM SIGHTINGS")
    ev, sg = build()
    b.insert_events(ev)
    b.insert_sightings(sg)
    print(f"Seeded {len(ev)} events and {len(sg)} sightings into {b.name}.")
