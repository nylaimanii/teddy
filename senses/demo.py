"""Test every senses/ function without hardware.

    .venv/bin/python -m senses.demo            # all mock tests
    .venv/bin/python -m senses.demo detect vlm # just some
    .venv/bin/python -m senses.demo live       # real webcam + mic + speaker
"""
import sys
import time
from pathlib import Path

import numpy as np

from senses import vision, voice
from senses.vision import Vision

A = Path(__file__).resolve().parent / "assets"
results = []


def check(name, ok, got, secs=None):
    results.append((name, ok))
    t = f" ({secs:.2f}s)" if secs is not None else ""
    print(f"{'PASS' if ok else 'FAIL'}  {name}{t}: {got}", flush=True)


def timed(fn, *a):
    t = time.time()
    r = fn(*a)
    return r, time.time() - t


def test_detect():
    v = Vision(mock=True, source=A / "cats_remotes.jpg", log_sightings=False, background=False)
    dets, s = timed(v.detect)
    check("detect remotes", any(d["label"] == "remote" for d in dets), dets, s)
    hit, s = timed(v.find, "the TV remote")
    check("find('the TV remote') + boxed image", hit and hit["label"] == "remote" and Path(hit["image"]).exists(), hit, s)
    hit, s = timed(v.find, "cat")  # open vocabulary
    check("find('cat') open-vocab", hit is not None, hit, s)
    hit, s = timed(v.find, "my keys")
    check("find('my keys') -> None", hit is None, hit, s)
    v.close()


def test_point():
    v = Vision(mock=True, source=A / "zidane.jpg", log_sightings=False)
    time.sleep(1)
    g = v.gestures()
    check("point (zidane.jpg points right)", g and g["type"] == "point" and g["x"] > 0.7, g)
    check("not fallen", not v.person_fallen(), False)
    v.close()


def test_fallen():
    v = Vision(mock=True, source=A / "fallen.jpg", log_sightings=False)
    time.sleep(0.5)
    early = v.person_fallen()
    time.sleep(2.2)
    check("fallen only after 2s", (not early) and v.person_fallen(), (early, v.person_fallen()))
    v.close()


def _fake_hist(wrist_fn, n=18, fps=12):
    """Synthetic pose history: person facing camera, right wrist follows wrist_fn(i)."""
    base = np.zeros((17, 3))
    base[:, 2] = 0.9
    pts = {0: (320, 100), 5: (360, 170), 6: (280, 170), 7: (380, 250), 8: (250, 240),
           9: (385, 320), 11: (350, 330), 12: (290, 330)}
    for i, p in pts.items():
        base[i, :2] = p
    hist = []
    for i in range(n):
        k = base.copy()
        (ex, ey), (wx, wy) = wrist_fn(i)
        k[8, :2], k[10, :2] = (ex, ey), (wx, wy)
        hist.append((i / fps, k, [240, 80, 400, 470], (640, 480)))
    return hist


def test_motion_gestures():
    wave = _fake_hist(lambda i: ((240, 140), (230 + 40 * np.sin(i * 1.4), 80)))
    g = vision._classify(wave)
    check("wave (synthetic)", g and g["type"] == "wave", g)
    beckon = _fake_hist(lambda i: ((250, 250), (250 + 70 * np.sin(i * 1.3) ** 2, 240 - 40 * abs(np.sin(i * 1.3)))))
    g = vision._classify(beckon)
    check("come_here (synthetic)", g and g["type"] == "come_here", g)
    still = _fake_hist(lambda i: ((250, 240), (245, 320)))
    g = vision._classify(still)
    check("arms at sides -> nothing", g is None, g)


def test_vlm():
    v = Vision(mock=True, source=A / "cats_remotes.jpg", log_sightings=False, background=False)
    v.warmup()
    r, s = timed(v.identify)
    check("identify", len(r) > 3, r, s)
    (r, src), s = timed(v._ask_vlm, "What animals are in this picture? One short sentence.", 512, 40, 0.01)
    check("gemini fallback when qwen is slow", src == "gemini" and r and "cat" in r.lower(), (r, src), s)
    v.close()
    v = Vision(mock=True, source=A / "medicine_label.jpg", log_sightings=False, background=False)
    r, s = timed(v.read_text)
    check("read_text", "amoxicillin" in r.lower(), r, s)
    v.close()


def test_sightings():
    got = []
    v = Vision(mock=True, source=A / "cats_remotes.jpg", background=False)
    v._log_sighting = lambda *a: got.append(a)
    import threading
    threading.Thread(target=v._sighting_loop, daemon=True).start()
    time.sleep(3)
    v.close()
    check("sighting loop logs each remote once", sorted(a[0] for a in got) == ["remote", "remote"], got)


def test_vitals():
    v = Vision(mock=True, source=A / "zidane.jpg", log_sightings=False, background=False)
    check("vitals -> {} (Presage dropped)", v.vitals() == {}, v.vitals())
    v.close()


def test_voice():
    voice.set_mock(True, audio_file=A / "hello.wav")
    r, s = timed(voice.listen)
    check("listen (hello.wav)", "keys" in r.lower(), r, s)
    voice.speak("I found your keys on the table!", mood="happy")
    voice.set_mock(False)
    got = []
    voice.set_sink(lambda audio, mime, text, mood: got.append((mime, len(audio))) or True)
    audio, s = timed(voice.speak, "Hi! I'm Teddy.")
    voice.set_sink(None)
    check("speak -> bytes sent to page sink", len(audio) > 1000 and got, got, s)


def live():
    """Real hardware: webcam + mic + speaker."""
    v = Vision()
    v.warmup()
    voice.speak("Hi! I'm Teddy. Wave at me!")
    print("detect:", v.detect())
    end = time.time() + 15
    while time.time() < end:
        g = v.gestures()
        if g:
            print("gesture:", g)
        if v.person_fallen():
            print("FALLEN!")
        time.sleep(0.1)
    print("identify:", timed(v.identify))
    print("read_text:", timed(v.read_text))
    print("vitals:", v.vitals())
    voice.speak("Say something to me.")
    print("heard:", voice.listen(5))


TESTS = {"detect": test_detect, "point": test_point, "fallen": test_fallen, "gestures": test_motion_gestures,
         "vlm": test_vlm, "sightings": test_sightings, "vitals": test_vitals, "voice": test_voice}

if __name__ == "__main__":
    args = sys.argv[1:]
    if args == ["live"]:
        live()
        sys.exit()
    for name in args or TESTS:
        try:
            TESTS[name]()
        except Exception as e:
            check(name, False, repr(e))
    print(f"\n{sum(ok for _, ok in results)}/{len(results)} passed")
    sys.exit(0 if all(ok for _, ok in results) else 1)
