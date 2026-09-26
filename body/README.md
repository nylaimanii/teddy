# body/ — the bear's movement

8 SG90 servos on an Arduino Uno (pins 2–9), driven over USB serial at 115200.
Angles are computed and eased on the Mac; `firmware/Bear.ino` just sets servos.

Use the shared venv: `.venv/bin/python` (Python 3.11 — see note below).

```python
from body.bear import Body

bear = Body()              # auto-finds the Uno; falls back to mock if absent
bear.pose("listen")        # returns immediately, he keeps moving
bear.point_at(0.8, 0.4)    # camera coords -> head + the arm on that side
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
- **The arms rest at 160 (left) and 20 (right), not 90.** Swinging them back
  toward 90 fouls the legs, so `LIMITS` stops them there: `arm_l` is capped at
  90 on the low side, `arm_r` at 90 on the high side. `sym(joint, offset)`
  measures from `HOME`, so a gesture written once still mirrors correctly.
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
