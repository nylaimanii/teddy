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

Try it: `python3 body/demo.py` (add `--mock` to run with no hardware, `--trace`
to print every servo write). One keypress per gesture, `x` stops him mid-move.

Notes for whoever's wiring this up:
- All the tunables are the config block at the top of `bear.py`: `LIMITS`
  (20–160 to start), `TRIM` (mechanical zero fudge), `DIRECTION` (flip a sign
  if a limb goes the wrong way), `LOOK_PAN`/`LOOK_TILT`, `MIRROR_CAMERA`.
- Never more than 3 servos start moving on the same instant — the 4xAA pack
  can't take more. Extra joints are staggered 60 ms apart automatically.
- Real hardware needs `pyserial` (installed in `.venv`); mock mode needs nothing.
- Run everything as `.venv/bin/python …` and install with
  `uv pip install --python .venv/bin/python <pkg>`. `.venv/bin/pip` belongs to a
  stray 3.14 install inside the same venv and writes where `.venv/bin/python`
  can't see it.
- The Uno enumerates as `/dev/cu.usbmodem1301`; `Body()` finds it on its own.
- Flash the Uno with `firmware/upload.sh` (compiles + uploads).
- **The Uno reboots whenever the serial port opens or closes**, which snaps
  every servo to 90. So `echo "0 40" > /dev/cu.usbmodem1301` never works: the
  board is still in its bootloader when the bytes land, and the shell closes
  the port before the sketch is running. Open the port, wait ~2 s, then send —
  that is what `Body.__init__` does. To hold a non-90 angle you must keep the
  port open, which is why `servo.py` idles instead of exiting.
- The firmware never detaches on its own; it only relaxes on an explicit `R`.
