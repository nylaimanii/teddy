"""Teddy's body: 8 SG90 servos on an Arduino Uno, driven over USB serial.

Contract (CONTRACTS.md):
    Body(port=None, mock=False)
    move(joint, angle); pose(name)  # neutral, wave, think, happy, sad, alert, listen
    look_at(x, y); point_at(x, y)   # 0..1 camera coords
    dance(seconds=10); cpr_beat(bpm=110, seconds=30); stop()

Motion is eased and runs on a background thread, so callers never block:
    bear.pose("listen")      # returns immediately, bear keeps moving
    bear.wait()              # ...unless you want to wait for it
    bear.stop()              # cancel whatever he's doing, hold still

Power note: we run on 4xAA, so no more than MAX_SIMULTANEOUS servos are ever
started at the same instant -- extra joints are staggered a few ms apart.
"""

import math
import sys
import threading
import time
from collections import deque

# ---------------------------------------------------------------- config ----
# Everything tweakable lives here. If the bear moves the wrong way on the real
# hardware, flip a sign in DIRECTION or swap a LOOK_* pair -- don't touch the
# gesture code.

JOINTS = {
    "head_pan": 0,
    "head_tilt": 1,
    "arm_l": 2,
    "arm_r": 3,
    "leg_l_side": 4,
    "leg_l_kick": 5,
    "leg_r_side": 6,
    "leg_r_kick": 7,
}

NEUTRAL = 90

# Per-joint safe travel. Start conservative; widen once the limbs are sewn in.
LIMITS = {
    "head_pan": (20, 160),
    "head_tilt": (20, 160),
    "arm_l": (20, 160),
    "arm_r": (20, 160),
    "leg_l_side": (20, 160),
    "leg_l_kick": (20, 160),
    "leg_r_side": (20, 160),
    "leg_r_kick": (20, 160),
}

# Mechanical zero fudge: added after the gesture math, before the limits.
# If an arm hangs 10 degrees off with everything at 90, put it here.
TRIM = {name: 0 for name in JOINTS}

# +1 means "bigger angle = the nice direction" (arm up, head up, head to his
# left, leg out/forward). Mirrored joints get -1 so gestures can be written once.
DIRECTION = {
    "head_pan": +1,    # +1 => bigger angle turns his head to HIS left
    "head_tilt": +1,   # +1 => bigger angle tilts his head UP
    "arm_l": +1,
    "arm_r": -1,
    "leg_l_side": +1,
    "leg_l_kick": -1,
    "leg_r_side": -1,
    "leg_r_kick": +1,
}

# Camera (0..1) -> head angles. x=0 is the left edge of the frame.
MIRROR_CAMERA = False   # True if senses/ hands us a selfie-mirrored frame
LOOK_PAN = (55, -55)    # offset from neutral at x=0 and x=1
LOOK_TILT = (35, -35)   # offset from neutral at y=0 (top) and y=1 (bottom)

MAX_SIMULTANEOUS = 3    # servos allowed to start moving on the same instant
STAGGER = 0.06          # seconds between one batch of 3 and the next
TICK = 0.02             # motion update period (50 Hz)
MIN_STEP = 1            # don't bother sending sub-degree changes

BAUD = 115200
PORT_GLOB = "/dev/cu.usbmodem*"   # the Uno; also matches usbserial on clones


def _ease(t):
    """Ease in/out. Servos hate step changes; this is the whole trick."""
    if t <= 0:
        return 0.0
    if t >= 1:
        return 1.0
    return 0.5 - 0.5 * math.cos(math.pi * t)


def _clamp(v, lo, hi):
    return lo if v < lo else hi if v > hi else v


# ------------------------------------------------------------------ body ----
class Body:
    def __init__(self, port=None, mock=False, baud=BAUD, trace=False):
        """port=None auto-detects the Uno. If it isn't there we fall back to
        mock rather than crashing -- a demo with a printing bear beats no demo."""
        self.mock = mock
        self.trace = trace
        self.ser = None
        self.port = None
        self.angles = {name: NEUTRAL for name in JOINTS}

        if not mock:
            self.port = port or self._find_port()
            if self.port:
                try:
                    import serial  # lazy: mock mode needs no pyserial
                except ImportError:
                    print("[bear] pyserial missing -- mock mode "
                          "(pip3 install pyserial)")
                    self.mock = True
                else:
                    try:
                        self.ser = serial.Serial(self.port, baud, timeout=0.2)
                        time.sleep(2.0)   # the Uno resets when the port opens
                        self.ser.reset_input_buffer()
                        print("[bear] connected on %s" % self.port)
                    except Exception as e:
                        print("[bear] serial failed on %s (%s) -- mock mode"
                              % (self.port, e))
                        self.mock = True
            else:
                print("[bear] no Arduino found -- mock mode")
                self.mock = True

        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)
        self._queue = deque()
        self._cancel = threading.Event()
        self._busy = False
        self._closing = False
        self._worker = threading.Thread(target=self._run, name="bear-body", daemon=True)
        self._worker.start()

        self.center()   # assert a known pose instead of assuming one

    # ---------------------------------------------------------- plumbing ----
    @staticmethod
    def _find_port():
        import glob
        hits = sorted(glob.glob(PORT_GLOB)) or sorted(glob.glob("/dev/cu.usbserial*"))
        return hits[0] if hits else None

    def _write(self, joint, angle):
        """Send one servo. Angle is already trimmed and clamped."""
        if self.angles.get(joint) == angle:
            return
        self.angles[joint] = angle
        if self.mock:
            if self.trace:
                print("[bear:mock] %-11s %3d" % (joint, angle))
            return
        try:
            self.ser.write(b"%d %d\n" % (JOINTS[joint], angle))
        except Exception as e:
            print("[bear] serial write failed (%s) -- mock mode" % e)
            self.mock = True

    def _resolve(self, joint):
        if isinstance(joint, int):
            for name, jid in JOINTS.items():
                if jid == joint:
                    return name
            raise ValueError("no servo with id %r" % joint)
        if joint not in JOINTS:
            raise ValueError("unknown joint %r" % joint)
        return joint

    def _safe(self, joint, angle):
        lo, hi = LIMITS[joint]
        return int(round(_clamp(angle + TRIM[joint], lo, hi)))

    def sym(self, joint, offset):
        """Neutral +/- offset, respecting the joint's mirror direction.
        sym('arm_r', 60) and sym('arm_l', 60) both mean 'arm up 60 degrees'."""
        joint = self._resolve(joint)
        return NEUTRAL + DIRECTION[joint] * offset

    # ------------------------------------------------------ motion engine ----
    def _sleep(self, seconds):
        """Cancellable sleep. Returns False if we were interrupted."""
        end = time.monotonic() + seconds
        while True:
            left = end - time.monotonic()
            if left <= 0:
                return not self._cancel.is_set()
            if self._cancel.wait(min(left, TICK)):
                return False

    def _transition(self, targets, duration=0.5):
        """Ease every joint in `targets` (name -> raw angle) to its target.

        Joints are started in batches of MAX_SIMULTANEOUS, STAGGER apart, so the
        battery never sees more than three stall currents at once.
        """
        tracks = []
        for i, (joint, raw) in enumerate(targets.items()):
            joint = self._resolve(joint)
            start = self.angles[joint]
            end = self._safe(joint, raw)
            if start == end:
                continue
            tracks.append((joint, start, end, (i // MAX_SIMULTANEOUS) * STAGGER))
        if not tracks:
            return self._sleep(duration)

        if self.mock and not self.trace:
            print("[bear:mock] " + ", ".join(
                "%s %d->%d" % (j, s, e) for j, s, e, _ in tracks
            ) + "  (%.2fs)" % duration)

        total = duration + max(d for _, _, _, d in tracks)
        t0 = time.monotonic()
        while True:
            if self._cancel.is_set():
                return False
            t = time.monotonic() - t0
            done = t >= total
            if done:
                t = total   # land exactly on target; keeps cpr_beat on tempo
            for joint, start, end, delay in tracks:
                p = _ease((t - delay) / duration) if duration > 0 else 1.0
                angle = int(round(start + (end - start) * p))
                if abs(angle - self.angles[joint]) >= MIN_STEP or p >= 1.0:
                    self._write(joint, angle)
            if done:
                return True
            time.sleep(min(TICK, total - t))

    def _run(self):
        while True:
            with self._cv:
                while not self._queue and not self._closing:
                    self._cv.wait()
                if self._closing:
                    return
                fn = self._queue.popleft()
                self._cancel.clear()
                self._busy = True
            try:
                fn()
            except Exception as e:
                print("[bear] routine crashed: %s" % e)
            finally:
                with self._cv:
                    self._busy = False
                    self._cv.notify_all()

    def _submit(self, fn, interrupt=True):
        with self._cv:
            if interrupt:
                self._queue.clear()
                self._cancel.set()
            self._queue.append(fn)
            self._cv.notify_all()
        return self

    def wait(self, timeout=None):
        """Block until the bear is done moving."""
        end = None if timeout is None else time.monotonic() + timeout
        with self._cv:
            while self._queue or self._busy:
                left = None if end is None else end - time.monotonic()
                if left is not None and left <= 0:
                    return False
                self._cv.wait(left if left is not None else 0.2)
        return True

    # -------------------------------------------------------- public API ----
    def move(self, joint, angle, duration=0.4, interrupt=True):
        """Ease one joint to an angle (degrees, before limits)."""
        joint = self._resolve(joint)
        return self._submit(lambda: self._transition({joint: angle}, duration), interrupt)

    def pose(self, name, interrupt=True):
        """Run one of the named gestures. See POSES / _GESTURES below."""
        name = name.lower().strip()
        gesture = _GESTURES.get(name)
        if gesture is None:
            raise ValueError("unknown pose %r (have: %s)"
                             % (name, ", ".join(sorted(_GESTURES))))
        if self.mock:
            print("[bear:mock] pose: %s" % name)
        return self._submit(lambda: gesture(self), interrupt)

    def look_at(self, x, y, duration=0.45, interrupt=True):
        """Point the head at a spot in the camera frame (x, y in 0..1)."""
        pan, tilt = self._head_for(x, y)
        return self._submit(
            lambda: self._transition({"head_pan": pan, "head_tilt": tilt}, duration),
            interrupt)

    def point_at(self, x, y, interrupt=True):
        """Look at it and raise the arm on that side."""
        x = _clamp(float(x), 0.0, 1.0)
        pan, tilt = self._head_for(x, y)
        near = "arm_l" if (x if not MIRROR_CAMERA else 1 - x) < 0.5 else "arm_r"
        far = "arm_r" if near == "arm_l" else "arm_l"
        # How far off-centre it is decides how high the arm goes.
        reach = 45 + 25 * abs(0.5 - x) * 2

        def routine():
            self._transition({
                "head_pan": pan,
                "head_tilt": tilt,
                far: self.sym(far, 0),
            }, 0.45)
            self._transition({near: self.sym(near, reach)}, 0.4)
            self._sleep(1.2)
            self._transition({near: self.sym(near, 0)}, 0.5)

        if self.mock:
            print("[bear:mock] point_at(%.2f, %.2f) -> %s" % (x, y, near))
        return self._submit(routine, interrupt)

    def dance(self, seconds=10, interrupt=True):
        """Ten seconds of waves: arms, then legs, then head, then all of him."""
        return self._submit(lambda: self._dance(seconds), interrupt)

    def cpr_beat(self, bpm=110, seconds=30, interrupt=True):
        """Compression metronome: head nod + both arms pumping on the beat."""
        return self._submit(lambda: self._cpr(bpm, seconds), interrupt)

    def center(self, settle=0.25, interrupt=True):
        """Command every servo to neutral, three at a time.

        Unlike pose("neutral") this always sends, even if we think he's already
        there -- which is what you want for lining up the horns, and on connect
        so his real pose matches our idea of it.
        """
        def routine():
            names = list(JOINTS)
            for i in range(0, len(names), MAX_SIMULTANEOUS):
                for joint in names[i:i + MAX_SIMULTANEOUS]:
                    self.angles[joint] = None   # force the write
                    self._write(joint, self._safe(joint, NEUTRAL))
                if not self._sleep(settle):
                    return

        if self.mock:
            print("[bear:mock] center: all 8 to %d" % NEUTRAL)
        return self._submit(routine, interrupt)

    def ping(self, timeout=1.0):
        """Ask the firmware to identify itself. True if it answers."""
        if self.mock or not self.ser:
            return False
        try:
            self.ser.reset_input_buffer()
            self.ser.write(b"P\n")
            end = time.monotonic() + timeout
            while time.monotonic() < end:
                line = self.ser.readline().decode("ascii", "replace").strip()
                if "BEAR" in line:
                    return True
        except Exception as e:
            print("[bear] ping failed: %s" % e)
        return False

    def stop(self):
        """Cancel everything and hold position."""
        with self._cv:
            self._queue.clear()
            self._cancel.set()
        if self.mock:
            print("[bear:mock] stop")
        return self

    def relax(self):
        """Stop, then let the servos go limp (quiet, and easy on the AAs)."""
        self.stop()
        self.wait(timeout=1.0)
        if self.mock:
            print("[bear:mock] relax")
        elif self.ser:
            try:
                self.ser.write(b"R\n")
            except Exception:
                pass
        return self

    def close(self):
        with self._cv:
            self._queue.clear()
            self._cancel.set()
            self._closing = True
            self._cv.notify_all()
        self._worker.join(timeout=1.0)
        if self.ser:
            try:
                self.ser.write(b"R\n")
                self.ser.close()
            except Exception:
                pass
            self.ser = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ---------------------------------------------------------- internals ----
    def _head_for(self, x, y):
        x = _clamp(float(x), 0.0, 1.0)
        y = _clamp(float(y), 0.0, 1.0)
        if MIRROR_CAMERA:
            x = 1.0 - x
        pan = NEUTRAL + DIRECTION["head_pan"] * (LOOK_PAN[0] + (LOOK_PAN[1] - LOOK_PAN[0]) * x)
        tilt = NEUTRAL + DIRECTION["head_tilt"] * (LOOK_TILT[0] + (LOOK_TILT[1] - LOOK_TILT[0]) * y)
        return pan, tilt

    def _both_arms(self, offset):
        return {"arm_l": self.sym("arm_l", offset), "arm_r": self.sym("arm_r", offset)}

    def _dance(self, seconds=10):
        # Four waves, each a share of the total. Arms -> legs -> head -> finale.
        arms, legs, head, finale = (0.32 * seconds, 0.28 * seconds,
                                    0.25 * seconds, 0.15 * seconds)

        # 1. arms: alternating raise, like he's doing the wave
        beat = arms / 6.0
        for _ in range(3):
            if not self._transition({"arm_l": self.sym("arm_l", 60),
                                     "arm_r": self.sym("arm_r", -10)}, beat):
                return
            if not self._transition({"arm_l": self.sym("arm_l", -10),
                                     "arm_r": self.sym("arm_r", 60)}, beat):
                return

        # 2. legs: side-to-side shuffle with a kick on each side (4 servos ->
        #    the engine staggers them into batches of 3 on its own)
        beat = legs / 4.0
        for side in ("l", "r", "l", "r"):
            other = "r" if side == "l" else "l"
            ok = self._transition({
                "leg_%s_side" % side: self.sym("leg_%s_side" % side, 45),
                "leg_%s_kick" % side: self.sym("leg_%s_kick" % side, 40),
                "leg_%s_side" % other: self.sym("leg_%s_side" % other, 0),
                "leg_%s_kick" % other: self.sym("leg_%s_kick" % other, 0),
            }, beat)
            if not ok:
                return

        # 3. head: look around, bob along
        beat = head / 4.0
        for pan, tilt in ((45, 15), (-45, -10), (45, 15), (0, 0)):
            if not self._transition({"head_pan": self.sym("head_pan", pan),
                                     "head_tilt": self.sym("head_tilt", tilt)}, beat):
                return

        # 4. finale: arms up, head up, hold, then home
        if not self._transition(dict(self._both_arms(70),
                                     head_tilt=self.sym("head_tilt", 25)),
                                finale * 0.45):
            return
        if not self._sleep(finale * 0.25):
            return
        self._neutral(finale * 0.3)

    def _cpr(self, bpm=110, seconds=30):
        period = 60.0 / max(40.0, min(160.0, float(bpm)))
        down, up = period * 0.4, period * 0.6
        beats = max(1, int(seconds / period))
        # Ready position: arms out front, head slightly down, looking at "them".
        if not self._transition(dict(self._both_arms(35),
                                     head_tilt=self.sym("head_tilt", -10)), 0.5):
            return
        for i in range(beats):
            # press: arms down + head nods down (3 servos exactly)
            if not self._transition(dict(self._both_arms(10),
                                         head_tilt=self.sym("head_tilt", -25)), down):
                return
            if not self._transition(dict(self._both_arms(35),
                                         head_tilt=self.sym("head_tilt", -5)), up):
                return
        self._neutral(0.6)

    def _neutral(self, duration=0.6):
        return self._transition({name: NEUTRAL for name in JOINTS}, duration)


# -------------------------------------------------------------- gestures ----
# Each takes the Body. They run on the worker thread and should bail out as soon
# as a _transition/_sleep returns False (that means stop() was called).

def _g_neutral(b):
    b._neutral(0.6)


def _g_wave(b):
    b._transition({"arm_r": b.sym("arm_r", 65),
                   "head_tilt": b.sym("head_tilt", 12)}, 0.45)
    for _ in range(3):
        if not b._transition({"arm_r": b.sym("arm_r", 40),
                              "head_pan": b.sym("head_pan", -12)}, 0.22):
            return
        if not b._transition({"arm_r": b.sym("arm_r", 68),
                              "head_pan": b.sym("head_pan", 12)}, 0.22):
            return
    b._transition({"arm_r": NEUTRAL, "head_pan": NEUTRAL, "head_tilt": NEUTRAL}, 0.5)


def _g_think(b):
    # Head cocked, one paw up by the chin, then a slow "hmm" sway.
    b._transition({"head_tilt": b.sym("head_tilt", 18),
                   "head_pan": b.sym("head_pan", 28),
                   "arm_r": b.sym("arm_r", 55)}, 0.7)
    for _ in range(2):
        if not b._transition({"head_pan": b.sym("head_pan", 14)}, 0.9):
            return
        if not b._transition({"head_pan": b.sym("head_pan", 32)}, 0.9):
            return


def _g_happy(b):
    b._transition(dict(b._both_arms(65), head_tilt=b.sym("head_tilt", 25)), 0.35)
    for _ in range(2):
        if not b._transition(dict(b._both_arms(40),
                                  head_pan=b.sym("head_pan", -18)), 0.2):
            return
        if not b._transition(dict(b._both_arms(68),
                                  head_pan=b.sym("head_pan", 18)), 0.2):
            return
    b._transition(dict(b._both_arms(0), head_pan=NEUTRAL,
                       head_tilt=b.sym("head_tilt", 8)), 0.5)


def _g_sad(b):
    # Everything slow and heavy: head down, arms hanging.
    b._transition(dict(b._both_arms(-40), head_tilt=b.sym("head_tilt", -35)), 1.4)
    if not b._sleep(0.6):
        return
    b._transition({"head_pan": b.sym("head_pan", -16)}, 1.2)


def _g_alert(b):
    # Snap upright, then a quick scan left/right.
    b._transition({"head_tilt": b.sym("head_tilt", 30),
                   "head_pan": NEUTRAL,
                   "arm_l": b.sym("arm_l", 25)}, 0.25)
    b._transition({"arm_r": b.sym("arm_r", 25)}, 0.2)
    for pan in (35, -35, 0):
        if not b._transition({"head_pan": b.sym("head_pan", pan)}, 0.3):
            return


def _g_listen(b):
    # The "I'm paying attention" tilt: ear toward you, chin slightly up, still.
    b._transition({"head_tilt": b.sym("head_tilt", 14),
                   "head_pan": b.sym("head_pan", 22),
                   "arm_l": b.sym("arm_l", -18),
                   "arm_r": b.sym("arm_r", -18)}, 0.6)
    # A tiny settle so he looks alive rather than frozen.
    b._transition({"head_pan": b.sym("head_pan", 26)}, 1.1)


_GESTURES = {
    "neutral": _g_neutral,
    "wave": _g_wave,
    "think": _g_think,
    "happy": _g_happy,
    "sad": _g_sad,
    "alert": _g_alert,
    "listen": _g_listen,
}

POSES = sorted(_GESTURES)


if __name__ == "__main__":
    mock = "--mock" in sys.argv
    bear = Body(mock=mock)
    try:
        for name in POSES:
            bear.pose(name)
            bear.wait()
    finally:
        bear.close()
