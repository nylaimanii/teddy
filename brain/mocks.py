"""Stand-ins for Body / Vision / voice (CONTRACTS.md) so the brain runs with no hardware."""
import random
import sys
import time


def _p(*a):
    print("  [mock]", *a, flush=True)


class MockBody:
    def __init__(self, port=None, mock=True):
        self._stop = False

    def move(self, joint, angle):
        _p(f"move {joint} -> {angle}")

    def pose(self, name):
        _p(f"pose {name}")

    def look_at(self, x, y):
        _p(f"look_at ({x:.2f}, {y:.2f})")

    def point_at(self, x, y):
        _p(f"point_at ({x:.2f}, {y:.2f})")

    def dance(self, seconds=10):
        _p(f"dance {seconds}s")
        self._sleep(seconds)

    def cpr_beat(self, bpm=110, seconds=30):
        _p(f"cpr_beat {bpm}bpm {seconds}s")
        self._sleep(seconds)

    def stop(self):
        self._stop = True
        _p("stop")

    def _sleep(self, seconds):
        self._stop = False
        end = time.time() + min(seconds, 3)  # keep mock demos snappy
        while time.time() < end and not self._stop:
            time.sleep(0.1)


class MockVision:
    THINGS = {"keys": (0.78, 0.62), "glasses": (0.22, 0.40), "remote": (0.40, 0.75), "cup": (0.30, 0.66)}

    def __init__(self, cam_index=0, mock=True):
        self.fallen = False
        self.pending_gesture = None

    def frame(self):
        return None

    def detect(self):
        return [{"label": k, "x": x, "y": y, "conf": 0.9} for k, (x, y) in self.THINGS.items()]

    def find(self, query):
        q = (query or "").lower()
        for k, (x, y) in self.THINGS.items():
            if k in q or q in k:
                if random.random() < 0.5:  # sometimes it's out of view -> brain falls back to memory
                    return None
                return {"label": k, "x": x, "y": y, "conf": 0.9}
        return None

    def identify(self):
        return random.choice(["a red toy car", "a coffee mug", "a pair of reading glasses", "a banana"])

    def read_text(self):
        return "Dear Rose, happy birthday! We love you and we'll visit on Sunday. Love, Maya."

    def person_fallen(self):
        return self.fallen

    def gestures(self):
        g, self.pending_gesture = self.pending_gesture, None
        return g

    def vitals(self):
        return {"heart_rate": random.randint(66, 80), "breathing_rate": random.randint(12, 16)}


class MockVoice:
    """listen() reads from the keyboard when there's a terminal, so you can talk to Teddy by typing."""

    def __init__(self, interactive=None, default_reply="I'm okay Teddy"):
        self.interactive = sys.stdin.isatty() if interactive is None else interactive
        self.default_reply = default_reply

    def listen(self, seconds=5):
        if self.interactive:
            try:
                return input("you> ").strip()
            except EOFError:
                self.interactive = False
                return ""
        time.sleep(0.3)
        return self.default_reply

    def speak(self, text, mood="warm"):
        print(f"🧸 Teddy ({mood}): {text}", flush=True)
