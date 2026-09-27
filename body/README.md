# body/ — the bear's movement

8 SG90 servos on an Arduino Uno (pins 2–9), driven over USB serial at 115200.
Angles are computed and eased on the Mac; `firmware/Bear.ino` just sets servos.

Use the shared venv: `.venv/bin/python` (Python 3.11 — see note below).

```python
from body.bear import Body

bear = Body()              # auto-finds the Uno; falls back to mock if absent
bear.pose("listen")        # returns immediately, he keeps moving
bear.look_at(0.8, 0.4)     # face a point in the frame he can see RIGHT NOW
bear.point_at(0.8, 0.4)    # ...and raise the arm on the side it's really on
bear.dance(10)
bear.cpr_beat(bpm=110, seconds=30)
bear.stop()                # cancel and hold
bear.wait()                # block until he's done, if you want to
bear.relax()               # servos off — quiet, saves the AAs
bear.center()              # force all 8 to 90 (horn alignment / known start)
bear.ping()                # True if the firmware answers "BEAR OK"
bear.close()               # servos HOLD their angle; close(relax=True) to let go
```

Bench tool for fitting horns — centers and holds until ctrl-c:

```
.venv/bin/python body/servo.py              # all 8 to 90, hold
.venv/bin/python body/servo.py 0            # wiggle servo 0, back to 90, hold
.venv/bin/python body/servo.py head_pan 40  # one joint to one angle, hold
```

Poses: `neutral wave think happy sad alert listen`.

Check which way every joint goes — one joint at a time, named as it moves:

```
.venv/bin/python body/check.py            # all 8
.venv/bin/python body/check.py arm_l 0    # just these (name or servo id)
```

Try it: `python3 body/demo.py` (add `--mock` to run with no hardware, `--trace`
to print every servo write). One keypress per gesture, `x` stops him mid-move.

Notes for whoever's wiring this up:
- All the tunables are the config block at the top of `bear.py`: `HOME`,
  `LIMITS`, `TRIM` (mechanical zero fudge), `DIRECTION` (flip a sign if a limb
  goes the wrong way), `LOOK_PAN`/`LOOK_TILT`, `MIRROR_CAMERA`.
- **`look_at`/`point_at` use a fixed mapping, because the camera is bolted to
  his BODY** — the hat does not turn when his head does. A frame is therefore
  always in body coordinates, so a given `x` means the same real direction
  however his head is pointing, and the angle is computed from `HOME` every
  time. The scale is the camera's real field of view (49.6 x 38.2 deg for the
  C270), damped 10% by `LOOK_GAIN`, which bounds the whole mapping to +/-22
  deg — it structurally cannot walk to an end stop. `LOOK_SLACK` suppresses
  sub-2-degree twitches from noisy detections.
  `relative=True` (or `look_toward()`) switches to head-relative aiming, which
  is the right model only if the camera ever moves onto his head. That mode
  accumulates, so it carries `LOOK_RUNAWAY`/`look_stalled` to stop it walking
  to an end stop when the view doesn't change.
- **Motion is tuned for a bear with stuffing in it.** Durations come from
  `MAX_DEG_PER_SEC` (520, just under the SG90's ~600) via `travel_time()`, so
  every move runs as fast as the horn can actually follow. Gesture sizes come
  from `reach(joint, fraction)` — a fraction of the joint's real travel —
  rather than fixed degree offsets, so each joint swings as far as it safely
  can. `_overshoot()` drives past a target and falls back, which is what makes
  `wave` and `happy` read from across a room.
- **His head rests at `head_tilt` 110, not 90** — the webcam in his hat is
  heavy enough to pull his chin down. Everything measured from `HOME` shifts
  with it, including the `look_at` mapping, so a target at the middle of the
  frame still puts his face level rather than 20° low. The firmware boots to
  the same pose, so he holds his head up before the Mac even connects.
- **The arms rest at 160 (left) and 20 (right), not 90.** Swinging them back
  toward 90 fouls the legs, so `LIMITS` stops them there: `arm_l` is capped at
  90 on the low side, `arm_r` at 90 on the high side. `sym(joint, offset)`
  measures from `HOME`, so a gesture written once still mirrors correctly.
- **An arm move gets the battery to itself.** The arms push the most fabric
  and are the first thing to sag when the pack droops, so any move that mixes
  an arm with another joint is split into consecutive blocks — arms first,
  alone — and each arm block is followed by `ARM_SETTLE` (0.16 s) of hold so
  the servo can finish shoving the stuffing before it is asked for anything
  else. `ARMS_ONE_AT_A_TIME = True` makes the left and right arms take turns
  as well, if both together still sag. `cpr_beat` opts out of the hold
  (`strict=True`) because its tempo is the point.
- **The legs detach after every move** (`RELAX_AFTER_MOVE`). One of them
  buzzes and shakes while holding position and is glued in, so the firmware
  cuts them loose the moment a move finishes and re-attaches automatically on
  the next angle sent. They go limp between moves — that is intentional.
- Never more than 3 servos start moving on the same instant — the 4xAA pack
  can't take more. Extra joints are staggered 60 ms apart automatically.
- Real hardware needs `pyserial` (installed in `.venv`); mock mode needs nothing.
- Run everything as `.venv/bin/python …` and install with
  `uv pip install --python .venv/bin/python <pkg>`. `.venv/bin/pip` belongs to a
  stray 3.14 install inside the same venv and writes where `.venv/bin/python`
  can't see it.
- The Uno enumerates as `/dev/cu.usbmodem1301`; `Body()` finds it on its own.
- Flash the Uno with `firmware/upload.sh` (compiles + uploads, with retries),
  or open `firmware/Bear/` in the Arduino IDE — the folder matches the .ino.
- **The Uno reboots whenever the serial port opens or closes**, which snaps
  every servo to 90. So `echo "0 40" > /dev/cu.usbmodem1301` never works: the
  board is still in its bootloader when the bytes land, and the shell closes
  the port before the sketch is running. Open the port, wait ~2 s, then send —
  that is what `Body.__init__` does. To hold a non-90 angle you must keep the
  port open, which is why `servo.py` idles instead of exiting.
- The firmware never detaches on its own; it only relaxes on an explicit `R`.

## Power — read this before blaming the code

**The servos must have their own 4xAA pack. They cannot run off the Uno's 5V
pin.** When the Uno is USB-powered, its 5V pin is fed from the USB line through
a 500 mA polyfuse. One SG90 draws ~250–400 mA while moving and stalls near
700 mA; eight of them are several amps. The fuse trips, the board browns out,
and the symptoms look like software bugs:

- serial goes silent, `ping()` returns nothing
- an orphaned `avrdude` keeps the port busy after a failed upload, so the
  next attempt fails for a different reason than the first (`upload.sh`
  clears these now; `lsof /dev/cu.usbmodem*` shows who holds the port)
- `arduino-cli upload` fails with `not in sync: resp=0x00`
- the USB port vanishes mid-upload and re-enumerates seconds later

Wiring: battery **+** to the servo V+ rail, battery **−** to the servo ground
rail **and** to an Arduino GND pin — the grounds must be common or the signal
wires have no reference. Signal wires go to pins 2–9. Do **not** feed 6 V into
the Arduino's 5 V pin; leave the Uno on USB.

4×AA alkaline is 6 V fresh, within the SG90's 4.8–6 V range, and sags under
load — which is the other reason `MAX_SIMULTANEOUS` is 3. A 470–1000 µF
capacitor across the servo rail smooths the current spikes.
