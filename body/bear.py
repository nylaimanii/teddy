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

# Where each joint rests. The arms hang out at 160/20 rather than 90: swinging
# them back toward 90 fouls the legs, so 90 is the *end* of their travel, not
# the middle of it.
HOME = {name: NEUTRAL for name in JOINTS}
HOME["arm_l"] = 160
HOME["arm_r"] = 20
# The webcam in his hat is heavy and pulls his chin down, so his head rests
# 20 degrees up from centre. Everything measured from home -- neutral, idle,
# center(), and the look_at mapping -- shifts with it, so "level" stays level.
HOME["head_tilt"] = 110

# Per-joint safe travel. The arm ranges stop at 90 on purpose -- past that they
# hit the legs. They get a little headroom beyond home so a gesture can still
# push slightly outward.
LIMITS = {
    "head_pan": (20, 160),
    "head_tilt": (20, 160),
    "arm_l": (90, 180),     # 90 = hard wall (fouls the legs); 160 = home
    "arm_r": (0, 90),       # mirrored: 90 = hard wall; 20 = home
    "leg_l_side": (20, 160),
    "leg_l_kick": (20, 160),
    "leg_r_side": (20, 160),
    "leg_r_kick": (20, 160),
}

# Mechanical zero fudge: added after the gesture math, before the limits.
# If an arm hangs 10 degrees off with everything at home, put it here.
TRIM = {name: 0 for name in JOINTS}

# +1 means "bigger angle = the nice direction". For the arms this points from
# home toward 90 (their only usable travel), so sym("arm_l", 60) and
# sym("arm_r", 60) both mean "swing the arm 60 degrees off its rest position".
DIRECTION = {
    "head_pan": +1,    # +1 => bigger angle turns his head to HIS left
    "head_tilt": +1,   # +1 => bigger angle tilts his head UP
    "arm_l": -1,       # home 160, swings down toward 90
    "arm_r": +1,       # home  20, swings down toward 90
    "leg_l_side": +1,
    "leg_l_kick": -1,
    "leg_r_side": -1,
    "leg_r_kick": +1,
}

# These buzz and shake when they hold position (and one is glued in, so it
# can't be swapped), so we cut them loose the moment a move finishes. The
# firmware re-attaches a servo automatically when we next send it an angle.
RELAX_AFTER_MOVE = {"leg_l_side", "leg_l_kick", "leg_r_side", "leg_r_kick"}

# The arms have the hardest job in the bear: they push the most fabric and
# stuffing, and they are the first thing to look weak when the battery sags.
# So an arm move gets the pack to itself -- no other servo moves with it -- and
# a moment of dwell afterwards to finish shoving the fabric out of the way
# before we ask it for anything else.
ARM_JOINTS = {"arm_l", "arm_r"}

# Joints that get the battery to themselves. Add "head_pan" here if the head
# turns out to be stalling in the fabric rather than slipping -- it is one
# small servo dragging a whole stuffed head around, the same problem the arms
# have. Costs pace: every gesture mixing that joint with another gets split.
EXCLUSIVE_JOINTS = set(ARM_JOINTS)
ARM_EXCLUSIVE = True    # these never move at the same instant as anything else
ARM_SETTLE = 0.16       # seconds of hold after every arm move
ARMS_ONE_AT_A_TIME = False   # True = left and right arms take turns too
                             # (flip this if both-arms-at-once still sags)

# Camera (0..1) -> head angles. senses/ uses x to the RIGHT and y DOWN
# (confirmed with Agent B), i.e. x=0 is the left edge, y=0 is the top.
#
# The camera is bolted to his BODY, not his head -- the hat does not turn when
# he does. So a frame is always in body coordinates: a given x means the same
# real-world direction no matter where his head happens to be pointing, and
# the mapping from frame position to head angle is fixed. That is what
# look_at/point_at do by default (relative=False).
#
# The scale is the camera's actual field of view, not a guess: a Logitech C270
# is 60 deg diagonal on a 4:3 sensor -> 49.6 x 38.2 degrees, so something at
# the very edge of frame is only ~25 deg off his centre line. The old +/-55
# mapping turned his head twice as far as the thing actually was.
CAMERA_FOV_X = 49.6
CAMERA_FOV_Y = 38.2
MIRROR_CAMERA = False   # True if senses/ ever hands us a selfie-mirrored frame
LOOK_GAIN = 0.9         # deliberately under-turn by 10%; overshooting and
                        # correcting looks worse than creeping up on it
LOOK_SLACK = 2.0        # don't bother re-commanding a move under this many
                        # degrees -- stops jitter from noisy detections

# Only used by look_at(..., relative=True), which treats the frame as an
# offset from where his head already points. That is the right model for a
# HEAD-mounted camera; keep it in case the camera ever moves onto the head.
# Because each call adds to the last, it can run away if the view doesn't
# actually change, so it carries the guard below. The default fixed mapping
# cannot run away: it is computed from HOME every time and is structurally
# bounded to +/-22 degrees.
LOOK_CENTRED = 0.12     # |x-0.5| within this counts as "facing it"
LOOK_RUNAWAY = 55.0     # degrees of same-direction turn before we call it stalled

MAX_SIMULTANEOUS = 3    # servos allowed to start moving on the same instant
STAGGER = 0.05          # seconds between one batch of 3 and the next

# He is inside a stuffed bear now, and the fabric fights every move, so we
# drive close to the servo's top speed and let the SERVO be the thing that
# lags -- not our easing curve. An SG90 is ~0.1s per 60 degrees unloaded
# (600 deg/s); we aim a little under that so the commanded motion is still
# something the horn can actually follow.
MAX_DEG_PER_SEC = 520
MIN_DURATION = 0.07     # floor, so tiny moves still get a real ramp
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
        self.angles = dict(HOME)

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

        self._look_drift = 0.0       # accumulated relative turn since he last
        self._look_stalled = False   # actually faced something
        self._io_lock = threading.Lock()   # one writer on the wire at a time
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
            with self._io_lock:
                self.ser.write(b"%d %d\n" % (JOINTS[joint], angle))
        except Exception as e:
            print("[bear] serial write failed (%s) -- mock mode" % e)
            self.mock = True

    def _detach(self, joints):
        """Tell the firmware to cut these servos loose so they stop buzzing.
        Sending them an angle later re-attaches them automatically."""
        for joint in joints:
            if self.mock:
                print("[bear:mock] %-11s detach (relax)" % joint)
            elif self.ser:
                try:
                    with self._io_lock:
                        self.ser.write(b"D %d\n" % JOINTS[joint])
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
        """Home +/- offset, respecting the joint's mirror direction.
        sym('arm_r', 60) and sym('arm_l', 60) both mean the same swing."""
        joint = self._resolve(joint)
        return HOME[joint] + DIRECTION[joint] * offset

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

    def reach(self, joint, fraction=1.0):
        """Angle `fraction` of the way from HOME to the far end of this joint's
        travel. fraction=+1 goes all the way in the joint's + direction, -1 all
        the way the other way, and both are mirrored, so reach('arm_l', 1) and
        reach('arm_r', 1) are the same swing on opposite sides.

        This is how gestures get their size: ask for a fraction of what the
        joint actually has, rather than guessing a degree offset that might be
        timid on one joint and slam into a limit on another.
        """
        joint = self._resolve(joint)
        home = HOME[joint]
        lo, hi = LIMITS[joint]
        plus_end = hi if DIRECTION[joint] > 0 else lo
        minus_end = lo if DIRECTION[joint] > 0 else hi
        end = plus_end if fraction >= 0 else minus_end
        return home + (end - home) * abs(fraction)

    def travel_time(self, targets, scale=1.0):
        """How long the biggest move in `targets` needs at near-top speed."""
        worst = 0
        for joint, raw in targets.items():
            joint = self._resolve(joint)
            now = self.angles.get(joint)
            if now is None:
                continue
            worst = max(worst, abs(self._safe(joint, raw) - now))
        return max(MIN_DURATION, worst / MAX_DEG_PER_SEC) * scale

    def _snap(self, targets, scale=1.0):
        """Move as fast as the servos can reasonably go."""
        return self._transition(targets, self.travel_time(targets, scale))

    def _overshoot(self, targets, past=15, settle=0.07, scale=1.0):
        """Drive past the target, then fall back into it. The little bounce is
        what makes a gesture read from across a room. Limits still apply, so
        this can never push an arm through the 90-degree wall."""
        over = {}
        for joint, raw in targets.items():
            joint = self._resolve(joint)
            end = self._safe(joint, raw)
            now = self.angles.get(joint)
            over[joint] = end if now is None or end == now else \
                self._safe(joint, end + (past if end > now else -past))
        if not self._snap(over, scale):
            return False
        return self._transition(targets, settle)

    def _transition(self, targets, duration=0.5, strict=False):
        """Ease every joint in `targets` to its target, giving the arms the
        power pack to themselves.

        If a move mixes arms with anything else, it is split into consecutive
        blocks -- arms first, then the rest -- so an arm never shares the
        battery with another servo. Each arm block is followed by ARM_SETTLE
        seconds of hold so the servo can finish pushing the fabric.

        strict=True keeps the total inside `duration` and skips the hold, for
        callers whose timing is the point (cpr_beat's metronome).
        """
        if not ARM_EXCLUSIVE:
            return self._move_block(targets, duration)

        blocks = self._split_for_arms(targets)
        if len(blocks) == 1 and not (blocks[0][1] and ARM_SETTLE):
            return self._move_block(blocks[0][0], duration)

        if strict:
            # Divide the caller's budget by how far each block has to travel,
            # so the beat lands exactly where it would have.
            spans = [max(1, self._span(t)) for t, _ in blocks]
            total = float(sum(spans))
            for (chunk, _), span in zip(blocks, spans):
                if not self._move_block(chunk, max(0.02, duration * span / total)):
                    return False
            return True

        for chunk, is_arm in blocks:
            span = self._span(chunk)
            if not self._move_block(chunk, max(MIN_DURATION,
                                               span / MAX_DEG_PER_SEC)):
                return False
            if is_arm and ARM_SETTLE and not self._sleep(ARM_SETTLE):
                return False
        return True

    def _span(self, targets):
        """Biggest distance any joint in `targets` has to travel, in degrees."""
        worst = 0
        for joint, raw in targets.items():
            joint = self._resolve(joint)
            now = self.angles.get(joint)
            if now is not None:
                worst = max(worst, abs(self._safe(joint, raw) - now))
        return worst

    def _split_for_arms(self, targets):
        """[(targets, exclusive?)] -- exclusive joints first, each alone.

        The arms count as one unit (they move as a pair in most gestures), but
        any OTHER exclusive joint gets a block of its own: the whole point is
        that it doesn't share the pack, and that includes not sharing it with
        the arms.
        """
        arms, other, rest = {}, {}, {}
        for joint, raw in targets.items():
            name = self._resolve(joint)
            if name in EXCLUSIVE_JOINTS:
                (arms if name in ARM_JOINTS else other)[name] = raw
            else:
                rest[name] = raw
        blocks = []
        if arms and ARMS_ONE_AT_A_TIME:
            blocks += [({j: v}, True) for j, v in arms.items()]
        elif arms:
            blocks.append((arms, True))
        blocks += [({j: v}, True) for j, v in other.items()]
        if rest:
            blocks.append((rest, False))
        return blocks or [(dict(targets), False)]

    def _move_block(self, targets, duration=0.5):
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

        legs = [j for j, _, _, _ in tracks if j in RELAX_AFTER_MOVE]
        total = duration + max(d for _, _, _, d in tracks)
        t0 = time.monotonic()
        while True:
            if self._cancel.is_set():
                self._detach(legs)
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
                self._detach(legs)
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

    def look_at(self, x, y, duration=0.45, interrupt=True, relative=False):
        """Turn his head to face a spot in the camera frame (x, y in 0..1).

        The camera is fixed to his body, so the frame is in body coordinates
        and the mapping is fixed: relative=False, the default. Pass
        relative=True only if the camera ever moves onto his head, where a
        frame means "relative to where I'm already looking".
        """
        def routine():
            pan, tilt = (self._head_toward(x, y) if relative
                         else self._head_for(x, y))
            if not relative and self._within_slack(pan, tilt):
                return True
            return self._transition({"head_pan": pan, "head_tilt": tilt}, duration)
        return self._submit(routine, interrupt)

    def look_toward(self, x, y, **kw):
        """look_at with the HEAD-relative mapping, for if the camera ever moves
        onto his head. The body-mounted camera we have wants plain look_at."""
        kw["relative"] = True
        return self.look_at(x, y, **kw)

    def _within_slack(self, pan, tilt):
        """True if he is already looking close enough that moving would just
        be jitter from a noisy detection."""
        now_p = self.angles.get("head_pan")
        now_t = self.angles.get("head_tilt")
        if now_p is None or now_t is None:
            return False
        return (abs(self._safe("head_pan", pan) - now_p) < LOOK_SLACK and
                abs(self._safe("head_tilt", tilt) - now_t) < LOOK_SLACK)

    def point_at(self, x, y, interrupt=True, relative=False):
        """Face it, then raise the arm on the side it's actually on.

        Which arm is decided from where his head ends up relative to his BODY,
        not from where the thing sat in the frame -- if his head was already
        turned, those are different answers.
        """
        def routine():
            pan, tilt = (self._head_toward(x, y) if relative
                         else self._head_for(x, y))
            home_pan = HOME["head_pan"]
            his_left = (pan - home_pan) * DIRECTION["head_pan"] >= 0
            near = "arm_l" if his_left else "arm_r"
            far = "arm_r" if near == "arm_l" else "arm_l"
            # The further round he had to turn, the higher the arm goes.
            span = abs(LIMITS["head_pan"][1] - home_pan) or 1
            lift = 0.55 + 0.45 * min(1.0, abs(pan - home_pan) / span)
            if self.mock:
                print("[bear:mock] point_at(%.2f, %.2f) -> pan %d, %s at %.0f%%"
                      % (x, y, pan, near, lift * 100))

            self._transition({"head_pan": pan, "head_tilt": tilt,
                              far: HOME[far]}, 0.45)
            self._snap({near: self.reach(near, -lift)})
            self._sleep(1.2)
            self._snap({near: HOME[near]})

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
            # non-arm joints in batches of three, then the arms on their own
            names = ([j for j in JOINTS if j not in EXCLUSIVE_JOINTS]
                     + sorted(EXCLUSIVE_JOINTS))
            for i in range(0, len(names), MAX_SIMULTANEOUS):
                batch = names[i:i + MAX_SIMULTANEOUS]
                if ARM_EXCLUSIVE and any(j in EXCLUSIVE_JOINTS for j in batch):
                    batch = [j for j in batch if j in EXCLUSIVE_JOINTS]
                for joint in batch:
                    self.angles[joint] = None   # force the write
                    self._write(joint, self._safe(joint, HOME[joint]))
                if not self._sleep(settle):
                    self._detach([j for j in names[:i + MAX_SIMULTANEOUS]
                                  if j in RELAX_AFTER_MOVE])
                    return
            self._detach([j for j in names if j in RELAX_AFTER_MOVE])

        if self.mock:
            print("[bear:mock] center: all 8 to home %s"
                  % ({j: HOME[j] for j in JOINTS},))
        return self._submit(routine, interrupt)

    def ping(self, timeout=1.0):
        """Ask the firmware to identify itself. True if it answers.

        Waits for any motion to finish first: the worker thread is writing
        servo commands on the same wire, and a reply can't be picked out of
        the middle of that.
        """
        if self.mock or not self.ser:
            return False
        self.wait(timeout=timeout)
        try:
            with self._io_lock:
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
                with self._io_lock:
                    self.ser.write(b"R\n")
            except Exception:
                pass
        return self

    def close(self, relax=False):
        """Shut down. By default the servos stay powered and HOLD their angle
        (that's what you want for fitting horns, and a limp bear looks dead).
        Pass relax=True to cut them loose."""
        with self._cv:
            self._queue.clear()
            self._cancel.set()
            self._closing = True
            self._cv.notify_all()
        self._worker.join(timeout=1.0)
        if self.ser:
            try:
                with self._io_lock:
                    if relax:
                        self.ser.write(b"R\n")
                        time.sleep(0.1)
                    self.ser.close()
            except Exception:
                pass
            self.ser = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ---------------------------------------------------------- internals ----
    @property
    def look_stalled(self):
        """True if relative aiming gave up because the head wasn't following.
        senses/ and brain/ can watch this to fall back to a search."""
        return self._look_stalled

    def reset_look(self):
        """Forget the runaway guard's history (call after fixing the head or
        deliberately moving him somewhere else)."""
        self._look_drift = 0.0
        self._look_stalled = False
        return self

    def _head_toward(self, x, y):
        """Where his head must go to FACE a point in the frame he can see now.

        The camera is on his head, so (x, y) is an offset from wherever he is
        already looking: dead centre means "don't move". This is the mapping
        that converges -- each call reduces the error instead of restarting
        from his body's centre line.
        """
        x = _clamp(float(x), 0.0, 1.0)
        y = _clamp(float(y), 0.0, 1.0)
        if MIRROR_CAMERA:
            x = 1.0 - x
        pan_now = self.angles.get("head_pan")
        tilt_now = self.angles.get("head_tilt")
        pan_now = HOME["head_pan"] if pan_now is None else pan_now
        tilt_now = HOME["head_tilt"] if tilt_now is None else tilt_now
        off_x, off_y = 0.5 - x, 0.5 - y

        # Already facing it: don't move, and forget any accumulated drift.
        if abs(off_x) <= LOOK_CENTRED and abs(off_y) <= LOOK_CENTRED:
            self._look_drift = 0.0
            self._look_stalled = False
            return pan_now, tilt_now

        d_pan = DIRECTION["head_pan"] * off_x * CAMERA_FOV_X * LOOK_GAIN
        d_tilt = DIRECTION["head_tilt"] * off_y * CAMERA_FOV_Y * LOOK_GAIN

        # Turning the same way again? Add it up. Turning back the other way
        # means the view really did change, so start the count over.
        if d_pan * self._look_drift < 0:
            self._look_drift = 0.0
        self._look_drift += d_pan

        if abs(self._look_drift) > LOOK_RUNAWAY:
            if not self._look_stalled:
                print("[bear] look: turned %.0f deg and it is still at x=%.2f "
                      "-- head isn't following, giving up (see look_stalled)"
                      % (abs(self._look_drift), x))
                self._look_stalled = True
            return pan_now, tilt_now

        return pan_now + d_pan, tilt_now + d_tilt

    def _head_for(self, x, y):
        """Fixed mapping: where in the world a frame position points, given a
        body-mounted camera. Always measured from HOME, so it cannot drift."""
        x = _clamp(float(x), 0.0, 1.0)
        y = _clamp(float(y), 0.0, 1.0)
        if MIRROR_CAMERA:
            x = 1.0 - x
        pan = HOME["head_pan"] + DIRECTION["head_pan"] * (0.5 - x) * CAMERA_FOV_X * LOOK_GAIN
        tilt = HOME["head_tilt"] + DIRECTION["head_tilt"] * (0.5 - y) * CAMERA_FOV_Y * LOOK_GAIN
        return pan, tilt

    def _both_arms(self, offset):
        return {"arm_l": self.sym("arm_l", offset), "arm_r": self.sym("arm_r", offset)}

    def _both_arms_reach(self, fraction):
        return {"arm_l": self.reach("arm_l", fraction),
                "arm_r": self.reach("arm_r", fraction)}

    def _dance(self, seconds=10):
        """Arms, then legs, then head, then all of him -- at full travel.

        Each phase runs to a deadline rather than a fixed number of beats,
        because the moves are now fast enough that a counted loop would finish
        early and leave him standing there. Sizes come from reach(), so every
        joint swings as far as it safely can; inside the stuffing anything
        smaller just disappears.
        """
        t0 = time.monotonic()
        arms_until = t0 + 0.30 * seconds
        legs_until = t0 + 0.58 * seconds
        head_until = t0 + 0.82 * seconds
        end = t0 + seconds

        # 1. arms: full-range alternating flap, as fast as they will go
        i = 0
        while time.monotonic() < arms_until:
            a, bb = (-1.0, 0.8) if i % 2 == 0 else (0.8, -1.0)
            if not self._snap({"arm_l": self.reach("arm_l", a),
                               "arm_r": self.reach("arm_r", bb),
                               "head_tilt": self.reach("head_tilt",
                                                       0.8 if i % 2 == 0 else 0.0)}):
                return
            i += 1

        # 2. legs: kicks swinging the WHOLE way through, not just out from home
        #    (4 servos -> the engine batches them 3 at a time, and they relax
        #    on their own once each move lands)
        i = 0
        while time.monotonic() < legs_until:
            side = "l" if i % 2 == 0 else "r"
            other = "r" if side == "l" else "l"
            ok = self._snap({
                "leg_%s_side" % side: self.reach("leg_%s_side" % side, 1.0),
                "leg_%s_kick" % side: self.reach("leg_%s_kick" % side, 1.0),
                "leg_%s_side" % other: self.reach("leg_%s_side" % other, -1.0),
                "leg_%s_kick" % other: self.reach("leg_%s_kick" % other, -1.0),
            })
            if not ok:
                return
            i += 1

        # 3. head: full bop, corner to corner
        i = 0
        while time.monotonic() < head_until:
            f = 1.0 if i % 2 == 0 else -1.0
            if not self._snap({"head_pan": self.reach("head_pan", f),
                               "head_tilt": self.reach("head_tilt", f)}):
                return
            i += 1

        # 4. finale: everything up, bounce, hold, home
        if not self._overshoot(dict(self._both_arms_reach(-1.0),
                                    head_tilt=self.reach("head_tilt", 1.0)), past=18):
            return
        if not self._sleep(max(0.0, end - time.monotonic() - 0.4)):
            return
        self._snap(dict(HOME), scale=1.6)

    def _cpr(self, bpm=110, seconds=30):
        """Compression metronome: head nod + both arms pumping on the beat.

        Every beat is timed against an absolute schedule rather than by adding
        up durations, so the per-move overhead (the arms now move in their own
        block, which costs a few ms each) can never accumulate into a drift.
        The tempo is the whole point of this gesture.
        """
        period = 60.0 / max(40.0, min(160.0, float(bpm)))
        beats = max(1, int(seconds / period))

        # Ready position: arms out front, head down, looking at "them".
        if not self._snap(dict(self._both_arms_reach(0.45),
                               head_tilt=self.reach("head_tilt", -0.2))):
            return

        t0 = time.monotonic()
        for i in range(beats):
            press_at = t0 + i * period
            release_at = press_at + period * 0.4
            next_at = t0 + (i + 1) * period

            # press: arms drive down hard, head nods with them
            down = max(0.05, release_at - time.monotonic())
            if not self._transition(dict(self._both_arms_reach(0.9),
                                         head_tilt=self.reach("head_tilt", -0.6)),
                                    down, strict=True):
                return
            up = max(0.05, next_at - time.monotonic())
            if not self._transition(dict(self._both_arms_reach(0.35),
                                         head_tilt=self.reach("head_tilt", -0.05)),
                                    up, strict=True):
                return
        self._snap(dict(HOME), scale=1.6)

    def _neutral(self, duration=0.6):
        return self._transition(dict(HOME), duration)


# -------------------------------------------------------------- gestures ----
# Each takes the Body. They run on the worker thread and bail out as soon as a
# _snap/_transition/_sleep returns False (that means stop() was called).
#
# Sizes are fractions of each joint's real travel via b.reach(), so a gesture
# is as big as the joint allows instead of a guessed number of degrees. He is
# inside a stuffed bear -- timid moves vanish into the stuffing.

def _g_neutral(b):
    b._snap(dict(HOME), scale=1.6)


def _g_wave(b):
    # Arm all the way up and away from the legs, then big fast flaps.
    b._snap({"arm_r": b.reach("arm_r", -1.0),
             "head_tilt": b.reach("head_tilt", 0.5)})
    for _ in range(4):
        if not b._snap({"arm_r": b.reach("arm_r", 0.35),
                        "head_pan": b.reach("head_pan", -0.35)}):
            return
        if not b._snap({"arm_r": b.reach("arm_r", -1.0),
                        "head_pan": b.reach("head_pan", 0.35)}):
            return
    b._overshoot({"arm_r": HOME["arm_r"], "head_pan": HOME["head_pan"],
                  "head_tilt": HOME["head_tilt"]}, past=12)


def _g_think(b):
    # Head cocked hard over, one paw up, then a slow deliberate sway.
    b._snap({"head_tilt": b.reach("head_tilt", 0.55),
             "head_pan": b.reach("head_pan", 0.85),
             "arm_r": b.reach("arm_r", -0.75)})
    for _ in range(2):
        if not b._snap({"head_pan": b.reach("head_pan", 0.35)}, scale=3.5):
            return
        if not b._snap({"head_pan": b.reach("head_pan", 0.95)}, scale=3.5):
            return


def _g_happy(b):
    # Both arms flung up, then full-range flapping with a bounce at the end.
    b._snap(dict(b._both_arms_reach(-1.0),
                 head_tilt=b.reach("head_tilt", 0.8)))
    for _ in range(3):
        if not b._snap(dict(b._both_arms_reach(0.5),
                            head_pan=b.reach("head_pan", -0.5))):
            return
        if not b._snap(dict(b._both_arms_reach(-1.0),
                            head_pan=b.reach("head_pan", 0.5))):
            return
    b._overshoot(dict(b._both_arms_reach(-0.2),
                      head_pan=HOME["head_pan"],
                      head_tilt=b.reach("head_tilt", 0.35)), past=18)


def _g_sad(b):
    # Head all the way down, arms dropped toward the legs. Quick to get there,
    # then it just hangs -- the stillness is the expression.
    b._snap({"head_tilt": b.reach("head_tilt", -1.0)}, scale=2.2)
    b._snap(b._both_arms_reach(0.85), scale=2.2)
    if not b._sleep(0.5):
        return
    b._snap({"head_pan": b.reach("head_pan", -0.55)}, scale=2.6)


def _g_alert(b):
    # Snap bolt upright, arms out, then a hard scan across the full sweep.
    b._overshoot({"head_tilt": b.reach("head_tilt", 1.0),
                  "head_pan": HOME["head_pan"],
                  "arm_l": b.reach("arm_l", -0.55)}, past=14)
    b._snap({"arm_r": b.reach("arm_r", -0.55)})
    for f in (1.0, -1.0, 0.0):
        if not b._snap({"head_pan": b.reach("head_pan", f)}):
            return


def _g_listen(b):
    # Ear cocked right over toward you, paws down, then hold still and just
    # breathe. Being still is the point -- but get there fast.
    b._snap({"head_tilt": b.reach("head_tilt", 0.45),
             "head_pan": b.reach("head_pan", 0.7),
             "arm_l": b.reach("arm_l", 0.25),
             "arm_r": b.reach("arm_r", 0.25)})
    b._snap({"head_pan": b.reach("head_pan", 0.85)}, scale=4.0)


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
