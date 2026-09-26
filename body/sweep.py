"""Step a joint through a range and hold at each angle, so you can watch and
say which one looks right. Used for finding true centre when a joint's
mechanical zero doesn't match the servo's.

    .venv/bin/python body/sweep.py head_pan 70 110 5      # 70..110 step 5
    .venv/bin/python body/sweep.py head_pan 70 110 5 3    # ...holding 3s each
    .venv/bin/python body/sweep.py arm_l 120 180 10

Nothing else moves during the sweep, and the port stays open the whole time --
closing it reboots the Uno, which snaps every servo back to its home pose.
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bear import Body, JOINTS, HOME, LIMITS  # noqa: E402


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    flags = [a for a in sys.argv[1:] if a.startswith("-")]
    if not args:
        args = ["head_pan", "70", "110", "5"]

    joint = args[0]
    lo, hi = int(args[1]), int(args[2])
    step = int(args[3]) if len(args) > 3 else 5
    hold = float(args[4]) if len(args) > 4 else 2.0

    bear = Body(mock="--mock" in flags)
    name = bear._resolve(int(joint) if joint.isdigit() else joint)
    if not bear.mock:
        print("firmware:", "BEAR OK" if bear.ping() else "NO REPLY")
    bear.wait(timeout=10)

    limit_lo, limit_hi = LIMITS[name]
    lo, hi = max(lo, limit_lo), min(hi, limit_hi)
    angles = list(range(lo, hi + 1, step))
    if angles[-1] != hi:
        angles.append(hi)

    print("\nsweeping %s (servo %d), home is %d, limits %d-%d"
          % (name, JOINTS[name], HOME[name], limit_lo, limit_hi))
    print("holding %.1fs on each of: %s\n" % (hold, angles))

    try:
        for angle in angles:
            bear.move(name, angle, duration=0.5)
            bear.wait(timeout=5)
            print("    >>>  %s = %d  <<<" % (name, angle), flush=True)
            time.sleep(hold)
        print("\nsweep done. Which angle looked right?")
        print("Back to home (%d); holding until ctrl-c so nothing flops."
              % HOME[name])
        bear.move(name, HOME[name], duration=0.5)
        bear.wait(timeout=5)
        if "--once" not in flags:
            while True:
                time.sleep(0.5)
    except KeyboardInterrupt:
        print("\nreleasing")
    finally:
        bear.close()


if __name__ == "__main__":
    main()
