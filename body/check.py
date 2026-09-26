"""Move each joint on its own and say which one is moving, so you can confirm
every joint goes the way it should.

    .venv/bin/python body/check.py            # all 8, one at a time
    .venv/bin/python body/check.py arm_l 0    # just these (name or servo id)
    .venv/bin/python body/check.py --fast     # shorter pauses
    .venv/bin/python body/check.py --mock     # no hardware

Nothing else moves while a joint is being tested, so there is never any doubt
about which servo you are looking at. Each joint returns to its home position
before the next one starts. Legs relax between moves -- that's deliberate.
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bear import Body, JOINTS, HOME, LIMITS, DIRECTION, RELAX_AFTER_MOVE  # noqa: E402

# What "positive" means for each joint, in plain words, so you can check the
# bear against the sentence rather than against an angle.
MEANING = {
    "head_pan":   ("turn his head to HIS LEFT",  "turn his head to HIS RIGHT"),
    "head_tilt":  ("tilt his head UP",           "tilt his head DOWN"),
    "arm_l":      ("swing his LEFT arm down/forward",  "swing his LEFT arm back up"),
    "arm_r":      ("swing his RIGHT arm down/forward", "swing his RIGHT arm back up"),
    "leg_l_side": ("swing his LEFT leg OUT",     "swing his LEFT leg IN"),
    "leg_l_kick": ("kick his LEFT leg FORWARD",  "kick his LEFT leg BACK"),
    "leg_r_side": ("swing his RIGHT leg OUT",    "swing his RIGHT leg IN"),
    "leg_r_kick": ("kick his RIGHT leg FORWARD", "kick his RIGHT leg BACK"),
}


def main():
    argv = sys.argv[1:]
    flags = [a for a in argv if a.startswith("-")]
    wanted = [a for a in argv if not a.startswith("-")]
    pause = 0.6 if "--fast" in flags else 1.4
    swing = 40

    bear = Body(mock="--mock" in flags)
    if not bear.mock:
        print("firmware:", "BEAR OK" if bear.ping() else
              "NO REPLY (is Bear.ino on the board?)")
    bear.wait(timeout=10)

    joints = [bear._resolve(int(w) if w.isdigit() else w) for w in wanted] or list(JOINTS)
    print("\nchecking %d joint(s); home first, then one joint at a time\n" % len(joints))

    try:
        for joint in joints:
            home = HOME[joint]
            lo, hi = LIMITS[joint]
            pos, neg = MEANING.get(joint, ("+", "-"))
            # Stay inside the limits, and don't ask a joint for travel it hasn't got.
            up = min(hi, home + swing) if DIRECTION[joint] > 0 else max(lo, home - swing)
            dn = max(lo, home - swing) if DIRECTION[joint] > 0 else min(hi, home + swing)

            print("=" * 58)
            print("servo %d  %s     home %d, limits %d-%d%s"
                  % (JOINTS[joint], joint, home, lo, hi,
                     "   (relaxes after each move)" if joint in RELAX_AFTER_MOVE else ""))
            for angle, what in ((up, pos), (home, "back to home"), (dn, neg), (home, "back to home")):
                if angle == bear.angles.get(joint):
                    continue
                print("   %-4d  should %s" % (angle, what))
                bear.move(joint, angle, duration=0.7)
                bear.wait(timeout=5)
                time.sleep(pause)
            print()
        print("=" * 58)
        print("done. Anything that moved the wrong way is a sign flip in")
        print("DIRECTION at the top of body/bear.py -- one line per joint.")
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        bear.stop()
        bear.close()


if __name__ == "__main__":
    main()
