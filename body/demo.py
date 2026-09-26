"""Keyboard demo for the bear's body. One keypress = one gesture.

    python3 body/demo.py            # auto: real Uno if plugged in, else mock
    python3 body/demo.py --mock     # always mock (prints moves)
    python3 body/demo.py --port /dev/cu.usbmodem11301
    python3 body/demo.py --trace    # print every single servo write

No enter key needed -- keys fire the moment you press them, and the bear keeps
moving while you press the next one, so 'x' really does stop him mid-dance.
"""

import os
import sys
import termios
import tty

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bear import Body, JOINTS  # noqa: E402

MENU = [
    ("n", "neutral",  "pose"),
    ("w", "wave",     "pose"),
    ("t", "think",    "pose"),
    ("h", "happy",    "pose"),
    ("s", "sad",      "pose"),
    ("a", "alert",    "pose"),
    ("l", "listen",   "pose"),
    ("d", "dance 10s",          "dance"),
    ("c", "cpr beat 110bpm",    "cpr"),
    ("1", "point at left",      "point_l"),
    ("2", "point at centre",    "point_c"),
    ("3", "point at right",     "point_r"),
    ("4", "look up / down / round", "sweep"),
    ("x", "STOP",     "stop"),
    ("r", "relax (servos off)", "relax"),
    ("q", "quit",     "quit"),
]


def banner(bear):
    where = "MOCK" if bear.mock else bear.port
    print("\n  teddy body demo  [%s]" % where)
    print("  " + "-" * 34)
    for key, label, _ in MENU:
        print("   %s  %s" % (key, label))
    print("  " + "-" * 34)
    print("  press a key...\n")


def getch():
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        return sys.stdin.read(1)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def main():
    argv = sys.argv[1:]
    port = None
    if "--port" in argv:
        port = argv[argv.index("--port") + 1]
    bear = Body(port=port, mock="--mock" in argv, trace="--trace" in argv)

    actions = {key: action for key, _, action in MENU}
    labels = {key: label for key, label, _ in MENU}
    banner(bear)
    try:
        while True:
            key = getch().lower()
            if key in ("", "\x03", "\x04"):   # EOF / ctrl-c / ctrl-d
                key = "q"
            action = actions.get(key)
            if action is None:
                if key == "?":
                    banner(bear)
                continue

            print("> %s" % labels[key])
            if action == "quit":
                break
            elif action == "pose":
                bear.pose(labels[key])
            elif action == "dance":
                bear.dance(10)
            elif action == "cpr":
                bear.cpr_beat(bpm=110, seconds=30)
            elif action == "point_l":
                bear.point_at(0.15, 0.4)
            elif action == "point_c":
                bear.point_at(0.5, 0.35)
            elif action == "point_r":
                bear.point_at(0.85, 0.4)
            elif action == "sweep":
                sweep = ((0.5, 0.05), (0.5, 0.95), (0.05, 0.5),
                         (0.95, 0.5), (0.5, 0.5))
                for i, (x, y) in enumerate(sweep):
                    bear.look_at(x, y, interrupt=(i == 0))
            elif action == "stop":
                bear.stop()
            elif action == "relax":
                bear.relax()
    finally:
        print("\nbye.")
        bear.close()


if __name__ == "__main__":
    main()
