"""Teddy's eyes. See CONTRACTS.md (senses/, Agent B).

    v = Vision()                                  # real webcam (TEDDY_CAM env picks the index)
    v = Vision(mock=True, source="photo.jpg")     # image or video file, no hardware

All coordinates are 0..1 camera coords (x right, y down).
"""
import base64
import json
import math
import os
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path

import cv2
import numpy as np
import requests

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
MODELS = HERE / "models"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
VLM = os.getenv("TEDDY_VLM", "qwen2.5vl:7b")

# canonical label -> YOLO-World prompt text
TRACKED = {
    "keys": "keys",
    "phone": "cell phone",
    "wallet": "wallet",
    "glasses": "eyeglasses",
    "remote": "remote control",
    "water bottle": "water bottle",
    "backpack": "backpack",
    "book": "book",
    "person": "person",
}
SYNONYMS = {
    "key": "keys", "car keys": "keys", "house keys": "keys", "keychain": "keys",
    "cell phone": "phone", "cellphone": "phone", "mobile": "phone", "iphone": "phone", "smartphone": "phone",
    "purse": "wallet", "eyeglasses": "glasses", "spectacles": "glasses", "reading glasses": "glasses",
    "sunglasses": "glasses", "tv remote": "remote", "remote control": "remote", "controller": "remote",
    "bottle": "water bottle", "water": "water bottle", "cup": "water bottle",
    "bag": "backpack", "school bag": "backpack", "bookbag": "backpack", "books": "book",
}
DETECT_CONF = 0.15
LOG_CONF = 0.25

# COCO keypoints
NOSE, L_SH, R_SH, L_EL, R_EL, L_WR, R_WR, L_HIP, R_HIP = 0, 5, 6, 7, 8, 9, 10, 11, 12
ARMS = {"left": (L_SH, L_EL, L_WR), "right": (R_SH, R_EL, R_WR)}
KP_CONF = 0.35

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}


def _log(*a):
    print("[vision]", *a, flush=True)


def _device():
    try:
        import torch
        return "mps" if torch.backends.mps.is_available() else "cpu"
    except Exception:
        return "cpu"


# ---------- shared models (loaded once per process, lazily) ----------
_models = {}
_model_lock = threading.Lock()
_gpu_lock = threading.RLock()  # MPS crashes if two threads run inference at once


def _world():
    with _model_lock:
        if "world" not in _models:
            from ultralytics import YOLOWorld
            m = YOLOWorld(str(MODELS / "yolov8s-worldv2.pt"))
            m.set_classes(list(TRACKED.values()))
            _models["world"] = m
        return _models["world"], _gpu_lock


def _pose():
    with _model_lock:
        if "pose" not in _models:
            from ultralytics import YOLO
            _models["pose"] = YOLO(str(MODELS / "yolov8n-pose.pt"))
        return _models["pose"]


def _gesture_recognizer():
    """MediaPipe gesture model for thumbs_up. Returns None if unavailable."""
    with _model_lock:
        if "mp" not in _models:
            _models["mp"] = None
            try:
                import mediapipe as mp
                from mediapipe.tasks.python import vision as mpv
                from mediapipe.tasks.python.core.base_options import BaseOptions
                path = MODELS / "gesture_recognizer.task"
                if not path.exists():
                    url = ("https://storage.googleapis.com/mediapipe-models/gesture_recognizer/"
                           "gesture_recognizer/float16/latest/gesture_recognizer.task")
                    path.write_bytes(requests.get(url, timeout=60).content)
                opts = mpv.GestureRecognizerOptions(base_options=BaseOptions(model_asset_path=str(path)),
                                                    num_hands=2)
                _models["mp"] = (mp, mpv.GestureRecognizer.create_from_options(opts))
            except Exception as e:
                _log("thumbs_up disabled (mediapipe unavailable):", e)
        return _models["mp"]


# ---------- local fallback for brain.snowflake.log_sighting ----------
LOCAL_SIGHTINGS = HERE / "local_sightings.json"


def _local_log_sighting(label, x, y):
    try:
        data = json.loads(LOCAL_SIGHTINGS.read_text()) if LOCAL_SIGHTINGS.exists() else []
    except Exception:
        data = []
    data.append({"label": label, "x": round(x, 3), "y": round(y, 3), "ts": time.time()})
    LOCAL_SIGHTINGS.write_text(json.dumps(data[-2000:]))


def local_last_seen(label):
    """Latest local sighting of label (used only when brain/ isn't ready)."""
    try:
        data = json.loads(LOCAL_SIGHTINGS.read_text())
    except Exception:
        return None
    hits = [d for d in data if d["label"] == label]
    return hits[-1] if hits else None


def _sighting_logger():
    try:
        from brain import snowflake
        fn = snowflake.log_sighting

        def log(label, x, y):
            try:
                fn(label, x, y)
            except Exception as e:
                _log("brain.log_sighting failed, saving locally:", e)
                _local_log_sighting(label, x, y)
        return log
    except Exception:
        _log("brain.snowflake not ready; sightings go to", LOCAL_SIGHTINGS.name)
        return _local_log_sighting


def normalize_query(query):
    q = query.lower().strip().rstrip("?.!")
    for prefix in ("my ", "the ", "a ", "an ", "your ", "our "):
        if q.startswith(prefix):
            q = q[len(prefix):]
    return SYNONYMS.get(q, q)


class Vision:
    def __init__(self, cam_index=None, mock=False, source=None, background=True, log_sightings=True):
        self.mock = mock
        self._lock = threading.Lock()
        self._frame = None
        self._running = True
        self._static = False
        self._threads = []

        if mock:
            source = source or os.getenv("TEDDY_MOCK_SOURCE") or str(HERE / "assets" / "bus.jpg")
            self.source = str(source)
            if Path(self.source).suffix.lower() in IMAGE_EXT:
                img = cv2.imread(self.source)
                if img is None:
                    raise FileNotFoundError(self.source)
                self._frame = img
                self._static = True
            else:
                self._start(self._video_loop)
        else:
            cam_index = int(os.getenv("TEDDY_CAM", 0)) if cam_index is None else cam_index
            self.source = cam_index
            self._start(self._camera_loop)
        self._wait_for_frame()

        # pose / gesture state
        self._pose_hist = deque(maxlen=60)  # (t, kpts[17,3] pixel coords + conf, box xyxy, (w, h))
        self._fallen_since = None
        self._last_person_t = 0
        self._thumb = None  # (t, x, y)
        self._last_gesture = {}
        self._pose_ready = threading.Event()

        # vitals
        self._presage = None
        self._vitals = {}
        self._vitals_t = 0

        self._log_sighting = _sighting_logger() if log_sightings else None
        self._last_logged = {}
        if background:
            self._start(self._pose_loop)
            if log_sightings:
                self._start(self._sighting_loop)

    # ---------- frames ----------
    def _start(self, fn):
        t = threading.Thread(target=fn, daemon=True)
        t.start()
        self._threads.append(t)

    def _camera_loop(self):
        cap = cv2.VideoCapture(self.source)
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
        fails = 0
        while self._running:
            ok, f = cap.read()
            if not ok:
                fails += 1
                if fails % 50 == 0:
                    _log(f"camera {self.source} not giving frames; retrying")
                    cap.release()
                    cap = cv2.VideoCapture(self.source)
                time.sleep(0.05)
                continue
            fails = 0
            with self._lock:
                self._frame = f
            self._feed_presage(f)
        cap.release()

    def _video_loop(self):
        cap = cv2.VideoCapture(self.source)
        if not cap.isOpened():
            _log("can't open video", self.source)
            return
        delay = 1.0 / (cap.get(cv2.CAP_PROP_FPS) or 30)
        while self._running:
            ok, f = cap.read()
            if not ok:
                cap.set(cv2.CAP_PROP_POS_FRAMES, 0)  # loop
                continue
            with self._lock:
                self._frame = f
            self._feed_presage(f)
            time.sleep(delay)
        cap.release()

    def _wait_for_frame(self, timeout=8):
        end = time.time() + timeout
        while self._frame is None and time.time() < end:
            time.sleep(0.05)
        if self._frame is None:
            _log("WARNING: no frames yet from", self.source)

    def frame(self):
        """Latest BGR frame (numpy array) or None."""
        with self._lock:
            return None if self._frame is None else self._frame.copy()

    def close(self):
        self._running = False
        if self._presage:
            try:
                self._presage.stdin.close()
                self._presage.terminate()
            except Exception:
                pass

    # ---------- objects ----------
    def _run_world(self, img, classes=None):
        model, lock = _world()
        with lock:
            if classes:
                model.set_classes(classes)
            try:
                res = model.predict(img, conf=DETECT_CONF, device=_device(), verbose=False)[0]
            finally:
                if classes:
                    model.set_classes(list(TRACKED.values()))
        return res

    @staticmethod
    def _to_dets(res, names):
        h, w = res.orig_shape
        out = []
        for box, cls, conf in zip(res.boxes.xyxy.tolist(), res.boxes.cls.tolist(), res.boxes.conf.tolist()):
            x1, y1, x2, y2 = box
            out.append({
                "label": names[int(cls)],
                "x": round((x1 + x2) / 2 / w, 3), "y": round((y1 + y2) / 2 / h, 3),
                "conf": round(conf, 2),
                "w": round((x2 - x1) / w, 3), "h": round((y2 - y1) / h, 3),
            })
        return sorted(out, key=lambda d: -d["conf"])

    def detect(self):
        """Tracked objects (and people) in view: [{"label","x","y","conf"}], best first."""
        img = self.frame()
        if img is None:
            return []
        return self._to_dets(self._run_world(img), list(TRACKED.keys()))

    def find(self, query):
        """Best match for a spoken thing ("my keys", "red mug") or None."""
        label = normalize_query(query)
        img = self.frame()
        if img is None:
            return None
        if label in TRACKED:
            hits = [d for d in self._to_dets(self._run_world(img), list(TRACKED.keys())) if d["label"] == label]
        else:  # open vocabulary: ask YOLO-World for exactly this thing
            hits = self._to_dets(self._run_world(img, classes=[label]), [label])
        return hits[0] if hits else None

    def _sighting_loop(self, every=2.0):
        while self._running:
            t0 = time.time()
            try:
                for d in self.detect():
                    if d["label"] == "person" or d["conf"] < LOG_CONF:
                        continue
                    now = time.time()
                    seen = [p for p in self._last_logged.get(d["label"], []) if now - p[2] < 30]
                    if not any(math.hypot(d["x"] - px, d["y"] - py) < 0.08 for px, py, _ in seen):
                        self._log_sighting(d["label"], d["x"], d["y"])
                        seen.append((d["x"], d["y"], now))
                    self._last_logged[d["label"]] = seen
            except Exception as e:
                _log("sighting loop error:", e)
            time.sleep(max(0.1, every - (time.time() - t0)))

    # ---------- VLM (Ollama) ----------
    def _ask_vlm(self, prompt, max_side=512, num_predict=60):
        img = self.frame()
        if img is None:
            return "I can't see anything right now."
        s = max_side / max(img.shape[:2])
        if s < 1:
            img = cv2.resize(img, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
        ok, jpg = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 85])
        try:
            r = requests.post(f"{OLLAMA_URL}/api/chat", timeout=60, json={
                "model": VLM, "stream": False, "keep_alive": "30m",
                "options": {"temperature": 0.2, "num_predict": num_predict},
                "messages": [{"role": "user", "content": prompt,
                              "images": [base64.b64encode(jpg.tobytes()).decode()]}],
            })
            r.raise_for_status()
            return r.json()["message"]["content"].strip()
        except Exception as e:
            _log("ollama error:", e)
            return "Hmm, my eyes are a little fuzzy right now. Can you try again?"

    def identify(self):
        """One short spoken sentence about what the person is showing the bear."""
        return self._ask_vlm(
            "You are a gentle teddy bear talking to a child or an older adult. "
            "Look at what the person is holding up or showing you (or the main thing in view). "
            "Say what it is in ONE short, friendly sentence under 15 words. "
            "Plain words only, no lists, no markdown.", num_predict=40)

    def read_text(self):
        """Reads visible text aloud-friendly (labels, letters, medicine bottles)."""
        return self._ask_vlm(
            "Read the text in this image for someone who cannot see it. "
            "If it is short, read it exactly. If it is long, say the most important parts "
            "(like a medicine name, dose, date, or who a letter is from) in under 40 words. "
            "Plain sentences for speaking aloud, no markdown. "
            "If there is no readable text, say: I don't see any words.", max_side=896, num_predict=90)

    def warmup(self):
        """Load all models so the first real call is fast."""
        _world(), _pose()
        self.detect()
        self._ask_vlm("Say ok.", max_side=64, num_predict=2)

    # ---------- pose: gestures + falls ----------
    def _pose_loop(self, hz=12):
        mp_every, n = 3, 0
        while self._running:
            t0 = time.time()
            try:
                self._pose_step(n % mp_every == 0)
            except Exception as e:
                _log("pose loop error:", e)
            n += 1
            self._pose_ready.set()
            time.sleep(max(0.0, 1 / hz - (time.time() - t0)))

    def _pose_step(self, run_mp):
        img = self.frame()
        if img is None:
            return
        h, w = img.shape[:2]
        t = time.time()
        model = _pose()
        with _gpu_lock:
            res = model.predict(img, conf=0.4, device=_device(), verbose=False)[0]
        if res.keypoints is not None and len(res.boxes):
            areas = [(b[2] - b[0]) * (b[3] - b[1]) for b in res.boxes.xyxy.tolist()]
            i = int(np.argmax(areas))  # closest (biggest) person
            k = res.keypoints.data[i].cpu().numpy()
            box = res.boxes.xyxy[i].tolist()
            self._pose_hist.append((t, k, box, (w, h)))
            self._last_person_t = t
            if _is_fallen_pose(k, box):
                self._fallen_since = self._fallen_since or t
            else:
                self._fallen_since = None
        elif self._lying_person(img, model):
            # nobody upright, but a person shows up when the frame is turned sideways: they're lying down
            self._last_person_t = t
            self._fallen_since = self._fallen_since or t
        elif t - self._last_person_t > 3:
            self._fallen_since = None

        if run_mp:
            rec = _gesture_recognizer()
            if rec:
                mp, g = rec
                rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
                out = g.recognize(mp.Image(image_format=mp.ImageFormat.SRGB, data=np.ascontiguousarray(rgb)))
                for gs, lms in zip(out.gestures, out.hand_landmarks):
                    if gs and gs[0].category_name == "Thumb_Up" and gs[0].score > 0.6:
                        self._thumb = (t, lms[0].x, lms[0].y)

    @staticmethod
    def _lying_person(img, model):
        """Pose models miss horizontal bodies, so look again with the frame rotated 90 degrees."""
        for rot in (cv2.ROTATE_90_COUNTERCLOCKWISE, cv2.ROTATE_90_CLOCKWISE):
            with _gpu_lock:
                r = model.predict(cv2.rotate(img, rot), conf=0.5, device=_device(), verbose=False)[0]
            if len(r.boxes):
                return True
        return False

    def person_fallen(self, hold=2.0):
        """True if the closest person has looked horizontal / head-at-hip-level for `hold`+ seconds."""
        self._pose_ready.wait(10)
        return self._fallen_since is not None and time.time() - self._fallen_since >= hold

    def gestures(self, window=1.5):
        """Latest gesture: {"type": wave|point|come_here|thumbs_up, "x", "y"} or None.
        For point, x,y is where the arm points. Otherwise it's the hand position."""
        self._pose_ready.wait(10)
        now = time.time()
        hist = [e for e in list(self._pose_hist) if now - e[0] <= window]
        g = None
        if hist:
            g = _classify(hist, static=self._static)
        if g is None and self._thumb and now - self._thumb[0] < 1.0:
            g = {"type": "thumbs_up", "x": round(self._thumb[1], 3), "y": round(self._thumb[2], 3)}
        if g is None:
            return None
        # debounce: don't report the same gesture again within 2s (static mock images always report)
        if not self._static and now - self._last_gesture.get(g["type"], 0) < 2.0:
            return None
        self._last_gesture[g["type"]] = now
        return g

    # ---------- vitals (Presage SmartSpectra via Node bridge) ----------
    def _start_presage(self):
        bridge = HERE / "presage" / "bridge.mjs"
        if not os.getenv("PRESAGE_API_KEY"):
            return "no PRESAGE_API_KEY in .env"
        if not (HERE / "presage" / "node_modules" / "@smartspectra").exists():
            return "run: cd senses/presage && npm install"
        try:
            self._presage = subprocess.Popen(["node", str(bridge)], stdin=subprocess.PIPE,
                                             stdout=subprocess.PIPE, cwd=str(HERE / "presage"),
                                             env=os.environ.copy())
        except Exception as e:
            return f"node failed: {e}"
        self._start(self._read_presage)
        return None

    def _read_presage(self):
        for line in self._presage.stdout:
            try:
                msg = json.loads(line)
            except Exception:
                continue
            if "heart_rate" in msg or "breathing_rate" in msg:
                self._vitals = {k: round(v, 1) for k, v in msg.items() if v is not None}
                self._vitals_t = time.time()
            elif "error" in msg:
                _log("presage:", msg["error"])

    def _feed_presage(self, img):
        p = self._presage
        if p is None or p.poll() is not None:
            return
        try:
            h, w = img.shape[:2]
            rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
            hdr = np.array([w, h], "<u4").tobytes() + np.array([time.time() * 1e6], "<f8").tobytes()
            p.stdin.write(hdr + rgb.tobytes())
            p.stdin.flush()
        except (BrokenPipeError, OSError):
            self._presage = None

    def vitals(self):
        """{"heart_rate", "breathing_rate"} from the webcam, or {} if not measured yet.
        First call starts Presage; it needs ~15-30s of a still, well-lit face to lock on."""
        if self._presage is None and not getattr(self, "_presage_err", None):
            self._presage_err = self._start_presage()
            if self._presage_err:
                _log("vitals unavailable:", self._presage_err)
            elif self._static:
                # feed the still image repeatedly so the SDK has a stream
                self._start(self._static_feed)
        if self._vitals and time.time() - self._vitals_t < 15:
            return dict(self._vitals)
        if self.mock and self._presage_err:
            return {"heart_rate": 72, "breathing_rate": 14, "mock": True}
        return {}

    def _static_feed(self):
        while self._running and self._presage:
            self._feed_presage(self.frame())
            time.sleep(1 / 30)


# ---------- pose geometry ----------
def _pt(k, i):
    return None if k[i][2] < KP_CONF else np.array(k[i][:2], float)


def _mid(a, b):
    if a is None:
        return b
    if b is None:
        return a
    return (a + b) / 2


def _angle(a, b, c):
    """Angle at b in degrees."""
    v1, v2 = a - b, c - b
    cos = np.dot(v1, v2) / (np.linalg.norm(v1) * np.linalg.norm(v2) + 1e-6)
    return math.degrees(math.acos(np.clip(cos, -1, 1)))


def _is_fallen_pose(k, box):
    sh = _mid(_pt(k, L_SH), _pt(k, R_SH))
    hip = _mid(_pt(k, L_HIP), _pt(k, R_HIP))
    nose = _pt(k, NOSE)
    bw, bh = box[2] - box[0], box[3] - box[1]
    if sh is not None and hip is not None:
        d = hip - sh
        torso_len = np.linalg.norm(d)
        tilt = math.degrees(math.atan2(abs(d[0]), abs(d[1]) + 1e-6))  # 0 = upright
        if tilt > 60:
            return True
        if nose is not None and abs(nose[1] - hip[1]) < 0.35 * torso_len and bw > 0.8 * bh:
            return True  # head down at hip level and body spread sideways
        return False
    return bw > 1.5 * bh  # only the box: much wider than tall


def _reversals(vals, min_step):
    """Count direction changes in a 1D series, ignoring jitter smaller than min_step."""
    n, direction, anchor = 0, 0, vals[0]
    for v in vals[1:]:
        if abs(v - anchor) < min_step:
            continue
        d = 1 if v > anchor else -1
        if direction and d != direction:
            n += 1
        direction, anchor = d, v
    return n


def _classify(hist, static=False):
    t, k, box, (w, h) = hist[-1]
    ls, rs = _pt(k, L_SH), _pt(k, R_SH)
    sw = np.linalg.norm(ls - rs) if ls is not None and rs is not None else (box[2] - box[0]) / 2
    sw = max(sw, 10)
    hip_y = _mid(_pt(k, L_HIP), _pt(k, R_HIP))
    hip_y = hip_y[1] if hip_y is not None else box[3]

    def norm(p):
        return {"x": round(float(np.clip(p[0] / w, 0, 1)), 3), "y": round(float(np.clip(p[1] / h, 0, 1)), 3)}

    for side, (S, E, W) in ARMS.items():
        series = [(_pt(e[1], S), _pt(e[1], E), _pt(e[1], W)) for e in hist]
        series = [s for s in series if all(p is not None for p in s)]
        if not series:
            continue
        sh, el, wr = series[-1]

        # wave: hand up above the shoulder, swinging side to side
        up = [s for s in series if s[2][1] < s[0][1]]
        if len(up) >= max(1, 0.6 * len(series)):
            xs = [s[2][0] - s[0][0] for s in up]  # relative to shoulder (ignores body sway)
            if static or (len(up) >= 5 and max(xs) - min(xs) > 0.4 * sw and _reversals(xs, 0.12 * sw) >= 2):
                return {"type": "wave", **norm(wr)}

        # come_here: hand at chest height, elbow bent, forearm curling toward / away from body
        if not static and len(series) >= 5 and sh[1] - 0.3 * sw < wr[1] < hip_y and _angle(sh, el, wr) < 130:
            ds = [np.linalg.norm(s[2] - s[0]) for s in series]
            if max(ds) - min(ds) > 0.3 * sw and _reversals(ds, 0.1 * sw) >= 2:
                return {"type": "come_here", **norm(wr)}

    # point: arm straight and held still, not hanging down
    for side, (S, E, W) in ARMS.items():
        recent = [e for e in hist if e[0] >= t - 0.6]
        pts = [(_pt(e[1], S), _pt(e[1], E), _pt(e[1], W)) for e in recent]
        pts = [p for p in pts if all(q is not None for q in p)]
        if not pts or (not static and len(pts) < 3):
            continue
        sh, el, wr = pts[-1]
        arm = wr - sh
        arm_len = np.linalg.norm(arm)
        down = math.degrees(math.acos(np.clip(arm[1] / (arm_len + 1e-6), -1, 1)))  # 0 = hanging straight down
        still = np.std([p[2][0] for p in pts]) + np.std([p[2][1] for p in pts]) < 0.25 * sw
        if _angle(sh, el, wr) > 150 and arm_len > 1.0 * sw and down > 40 and still:
            direction = arm / arm_len
            # walk out along the arm ray ~1.5 arm lengths, stopping at the frame edge
            target = wr.copy()
            for _ in range(int(1.5 * arm_len)):
                nxt = target + direction
                if not (0 <= nxt[0] < w and 0 <= nxt[1] < h):
                    break
                target = nxt
            return {"type": "point", **norm(target), "side": side}
    return None
