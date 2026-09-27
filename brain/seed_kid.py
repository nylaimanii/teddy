"""A week of Maya (7) and Teddy, for the Parent page demo. Writes Snowflake (EVENTS, MOODS, SIGHTINGS)
and Tiger Data (teddy_activity, teddy_moods) when TIGER_DATABASE_URL is set.

    python -m brain.seed_kid --reset     # wipe the old demo data first (both stores)

Story: Monday she gives up halfway through tying her shoes; Tuesday she gets it (badge!). Brushing teeth
Wednesday, a cat drawing Thursday, packing her backpack Friday, Rock Paper Scissors Saturday, and today she
ties her shoes again in half the time. Homework most afternoons, with a frustrated patch during subtraction
on Tuesday and a sad moment Wednesday evening, each met with a gentle check-in. Her stuff wanders around.
"""
import datetime as dt
import json
import random
import sys

from brain import flags
from brain import snowflake as sf

random.seed(11)
KID = flags.KID_NAME
TZ = dt.datetime.now().astimezone().tzinfo


def at(days_ago, h, m=0, s=0):
    d = dt.datetime.now().replace(microsecond=0) - dt.timedelta(days=days_ago)
    return d.replace(hour=h, minute=m, second=s)


HOWTOS = [  # (days_ago, hour, guide, skill, badge, steps, finished, minutes)
    (6, 16, "tie-shoes", "Tying shoes", "Tied My Shoes!", 7, False, 6),
    (5, 16, "tie-shoes", "Tying shoes", "Tied My Shoes!", 7, True, 9),
    (4, 19, "brush-teeth", "Brushing teeth", "Sparkly Teeth!", 7, True, 4),
    (3, 17, "draw-cat", "Drawing a cat", "Drew a Cat!", 7, True, 7),
    (2, 19, "pack-backpack", "Packing my backpack", "Packed & Ready!", 6, True, 5),
    (1, 11, "rock-paper-scissors", "Rock Paper Scissors", "Game Champ!", 5, True, 4),
    (0, 8, "tie-shoes", "Tying shoes", "Tied My Shoes!", 7, True, 4),
]
HOMEWORK = [  # (days_ago, hour, question, steps, wrong_tries)
    (6, 15, "what is 34 plus 28", 3, 1), (5, 15, "what's 45 minus 17", 2, 4), (4, 15, "what is 7 times 8", 3, 1),
    (3, 15, "what is 3/4 plus 1/4", 3, 2), (2, 15, "why do plants need sunlight?", 1, 1), (1, 10, "what is 56 divided by 8", 1, 0),
]
UPSETS = [  # (days_ago, h, m, feeling, seconds, helping_with)
    (5, 15, 12, "frustrated", 24, "Homework: what's 45 minus 17"),
    (4, 20, 40, "sad", 16, "hanging out"),
    (3, 15, 20, "frustrated", 11, "Homework: what is 3/4 plus 1/4"),
]
STUFF = {"keys": [("desk", .72, .58), ("door", .9, .5)], "shoes": [("door", .88, .78), ("bed", .2, .72)],
         "backpack": [("door", .86, .62), ("desk", .7, .66)], "bunny": [("bed", .22, .55), ("couch", .35, .6)],
         "remote": [("couch", .32, .66), ("tv", .5, .45)]}
LANDMARKS = {"desk": (.72, .6), "door": (.9, .45), "bed": (.2, .62), "couch": (.33, .62), "tv": (.5, .3)}


def build():
    ev, moods, sg, tiger_act, tiger_mood = [], [], [], [], []

    def e(t, kind, **data):
        if t > dt.datetime.now():
            return
        ev.append((sf._ts(t), kind, json.dumps(data)))
        tiger_act.append((t.replace(tzinfo=TZ), kind, data.get("skill"), data.get("duration_s"), data))

    def feel(t0, minutes, pattern=("calm", "calm", "happy", "calm", "happy")):
        for i in range(int(minutes * 3)):  # a reading every 20 s while Teddy and Maya are together
            t = t0 + dt.timedelta(seconds=20 * i)
            if t > dt.datetime.now():
                return
            label = random.choice(pattern)
            moods.append((sf._ts(t), KID, label, {"calm": "neutral", "happy": "happy", "sad": "sad",
                                                  "frustrated": "angry", "upset": "fearful"}[label], .8, None))
            tiger_mood.append((t.replace(tzinfo=TZ), label))

    for d, h, gid, skill, badge, n, finished, mins in HOWTOS:
        t = at(d, h, random.randint(0, 20))
        e(t, "howto_start", skill=skill, guide=gid)
        steps = n if finished else 4
        for i in range(steps):
            e(t + dt.timedelta(minutes=mins * (i + 1) / n), "howto_step", skill=skill, guide=gid, step=i + 1, of=n)
        end = t + dt.timedelta(minutes=mins)
        if finished:
            e(end, "skill_done", skill=skill, guide=gid, badge=badge, duration_s=mins * 60)
            e(end, "badge", skill=skill, badge=badge)
        else:
            e(end, "howto_stop", skill=skill, guide=gid, step=steps, of=n)
        feel(t, mins, ("calm", "happy", "happy", "calm") if finished else ("calm", "frustrated", "calm", "sad"))

    for d, h, q, steps, wrong in HOMEWORK:
        t = at(d, h, random.randint(0, 25))
        skill = "Math" if any(c.isdigit() for c in q) else "Homework"
        e(t, "homework_start", skill=skill, question=q, steps=steps)
        k = 0
        for i in range(steps):
            for _ in range(wrong if i == steps - 1 else 0):
                k += 1
                e(t + dt.timedelta(seconds=40 * k), "homework_try", skill=skill, question=q, step=i + 1, correct=False)
            k += 1
            e(t + dt.timedelta(seconds=40 * k), "homework_try", skill=skill, question=q, step=i + 1, correct=True)
        e(t + dt.timedelta(seconds=40 * k + 20), "homework_done", skill=skill, question=q, tries=k,
          duration_s=40 * k + 20)
        feel(t, (40 * k + 20) / 60 + 1, ("calm", "calm", "happy"))

    for d, h, m, feeling, secs, doing in UPSETS:
        t = at(d, h, m)
        for i in range(0, secs, 2):
            moods.append((sf._ts(t + dt.timedelta(seconds=i)), KID, feeling,
                          "angry" if feeling == "frustrated" else "sad", .85, doing))
            tiger_mood.append(((t + dt.timedelta(seconds=i)).replace(tzinfo=TZ), feeling))
        if secs >= 10:
            e(t + dt.timedelta(seconds=10), "mood_checkin", feeling=feeling, helping_with=doing)
        e(t + dt.timedelta(seconds=secs), "upset_moment", feeling=feeling, duration_s=secs, helping_with=doing,
          started=t.isoformat(timespec="seconds"), checked_in=secs >= 10)

    for d in range(6, -1, -1):  # stuff wandering around the house, and a few finds
        for obj, spots in STUFF.items():
            for _ in range(random.randint(1, 2)):
                t = at(d, random.randint(7, 20), random.randint(0, 59))
                if t > dt.datetime.now():
                    continue
                spot, x, y = random.choice(spots)
                sg.append((sf._ts(t), obj, x + random.uniform(-.03, .03), y + random.uniform(-.03, .03), .1, .1, None))
                lx, ly = LANDMARKS[spot]
                sg.append((sf._ts(t), spot, lx, ly, .25, .25, None))
    for d, h, obj in [(6, 7, "shoes"), (5, 7, "backpack"), (4, 18, "bunny"), (3, 7, "keys"), (2, 20, "remote"),
                      (1, 9, "bunny"), (0, 7, "shoes")]:
        e(at(d, h, 30), "find", skill="Finding my stuff", object=obj, found=random.choice(["now", "memory"]))
    # the keys this morning: on the desk, by the laptop spot
    sg.append((sf._ts(at(0, 7, 5) if at(0, 7, 5) < dt.datetime.now() else at(1, 20, 5)), "keys", .73, .59, .1, .1, None))
    sg.append((sf._ts(at(0, 7, 5) if at(0, 7, 5) < dt.datetime.now() else at(1, 20, 5)), "desk", .72, .6, .25, .25, None))
    for d, h in [(5, 20), (3, 20), (1, 19)]:
        e(at(d, h, 10), "read", skill="Reading", words=random.randint(12, 40))
    return ev, moods, sg, tiger_act, tiger_mood


def main(reset=False):
    b = sf.configure()
    if reset:
        for t in ("EVENTS", "SIGHTINGS"):
            b.query(f"DELETE FROM {t}")
        try:
            b.query("DELETE FROM MOODS")
        except Exception:
            pass
    b.query("CREATE TABLE IF NOT EXISTS MOODS (TS TIMESTAMP_NTZ, KID STRING, LABEL STRING, RAW STRING, CONF FLOAT, "
            "ACTIVITY STRING)" if b.name == "snowflake" else
            "CREATE TABLE IF NOT EXISTS MOODS (TS TEXT, KID TEXT, LABEL TEXT, RAW TEXT, CONF REAL, ACTIVITY TEXT)")
    ev, moods, sg, tact, tmood = build()
    b.insert_events(ev)
    b.insert_sightings(sg)
    for i in range(0, len(moods), 400):
        part = moods[i:i + 400]
        b.query("INSERT INTO MOODS (TS, KID, LABEL, RAW, CONF, ACTIVITY) VALUES "
                + ",".join(["(%s,%s,%s,%s,%s,%s)"] * len(part)), [v for r in part for v in r])
    print(f"Snowflake ({b.name}): {len(ev)} events, {len(moods)} mood check-ins, {len(sg)} sightings")
    if flags.tiger():
        from brain import tiger
        tiger.setup()
        c = tiger.conn()
        if reset:
            c.execute("TRUNCATE teddy_activity; TRUNCATE teddy_moods")
        with c.cursor() as cur:
            cur.executemany("INSERT INTO teddy_activity (time, kid, kind, skill, duration_s, detail) VALUES (%s,%s,%s,%s,%s,%s)",
                            [(t, KID, k, s, dur, json.dumps(dat)) for t, k, s, dur, dat in tact])
            cur.executemany("INSERT INTO teddy_moods (time, kid, label, conf, activity) VALUES (%s,%s,%s,%s,%s)",
                            [(t, KID, lbl, .8, None) for t, lbl in tmood])
        tiger.refresh()
        print(f"Tiger Data: {len(tact)} activity rows, {len(tmood)} mood rows (+ refreshed continuous aggregates)")


if __name__ == "__main__":
    main(reset="--reset" in sys.argv)
