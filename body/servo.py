"""Single-servo bench tool -- for centering and fitting horns.

    .venv/bin/python body/servo.py              # center all 8 and HOLD
    .venv/bin/python body/servo.py 0            # wiggle servo 0, back to 90, HOLD
    .venv/bin/python body/servo.py 0 40         # put servo 0 at 40 and HOLD
    .venv/bin/python body/servo.py head_pan 40  # same, by name
    ... add --mock to run with no hardware, --once to exit instead of holding

It holds the angle until you press ctrl-c, because the Uno reboots the moment
the serial port closes -- which snaps every servo back to 90. That reboot is
also why `echo "0 40" > /dev/cu.usbmodem1301` does nothing: the board is still
in its bootloader when the bytes arrive, and the shell closes the port before
the sketch is even running. Anything talking to the bear must open the port,
wait ~2s, and only then send.
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bear import Body, JOINTS, NEUTRAL  # noqa: E402


def main():
    flags = [a for a in sys.argv[1:] if a.startswith("-")]
    args = [a for a in sys.argv[1:] if not a.startswith("-") and not a.startswith("/dev/")]
    port = next((a for a in sys.argv[1:] if a.startswith("/dev/")), None)
    bear = Body(port=port, mock="--mock" in flags)
    if bear.mock:
        print("no hardware -- running mock")
    else:
        print("firmware:", "BEAR OK" if bear.ping() else
              "NO REPLY (is Bear.ino actually on the board?)")

    try:
        if not args:
            print("centering all 8 at %d" % NEUTRAL)
            bear.center()
        else:
            joint = args[0]
            joint = int(joint) if joint.isdigit() else joint
            name = bear._resolve(joint)
            if len(args) > 1:
                angle = int(args[1])
                print("%s (servo %d) -> %d" % (name, JOINTS[name], angle))
                bear.move(name, angle)
            else:
                print("wiggling %s (servo %d): 90 -> 40 -> 140 -> 90"
                      % (name, JOINTS[name]))
                for a in (40, 140, NEUTRAL):
                    bear.move(name, a, duration=0.6, interrupt=False)
        bear.wait(timeout=20)
        if "--once" in flags:
            return
        print("holding -- ctrl-c to let go")
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\nreleasing")
    finally:
        bear.close()   # holds; the board reboots to 90 as the port closes


if __name__ == "__main__":
    main()
