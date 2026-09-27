"""Teddy's eyes. See CONTRACTS.md (senses/, Agent B).

    v = Vision()                                  # real webcam (TEDDY_CAM env picks the index)
    v = Vision(mock=True, source="photo.jpg")     # image or video file, no hardware

All coordinates are 0..1 camera coords (x right, y down).
"""
import base64
import json
import math
import os
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
GEMINI_MODEL = os.getenv("TEDDY_GEMINI", "gemini-flash-latest")
VLM_DEADLINE = float(os.getenv("TEDDY_VLM_DEADLINE", 6))  # seconds before we give up on qwen
SNAPSHOT = HERE / "snapshots" / "find.jpg"

# canonical label -> YOLO-World prompt text
# (canonical label, YOLO-World prompt). A label can have several prompts; they share one label.
TRACKED_PROMPTS = [
    ("keys", "keys"),
    ("phone", "cell phone"),
    ("wallet", "wallet"),
    ("glasses", "eyeglasses"),
    ("remote", "remote control"),
    ("water bottle", "water bottle"),
    ("backpack", "backpack"),
    ("backpack", "school bag"),
    ("shoes", "shoe"),
    ("shoes", "sneaker"),
    ("book", "book"),
    ("bunny", "white stuffed bunny"),
    ("bunny", "stuffed animal"),
    ("bunny", "plush toy"),
    ("person", "person"),
]
LABELS = [label for label, _ in TRACKED_PROMPTS]
PROMPTS = [prompt for _, prompt in TRACKED_PROMPTS]
TRACKED = set(LABELS)
SYNONYMS = {
    "key": "keys", "car keys": "keys", "house keys": "keys", "keychain": "keys",
    "cell phone": "phone", "cellphone": "phone", "mobile": "phone", "iphone": "phone", "smartphone": "phone",
    "purse": "wallet", "eyeglasses": "glasses", "spectacles": "glasses", "reading glasses": "glasses",
    "sunglasses": "glasses", "tv remote": "remote", "remote control": "remote", "controller": "remote",
    "bottle": "water bottle", "water": "water bottle", "cup": "water bottle",
    "stuffed bunny": "bunny", "white stuffed animal": "bunny", "stuffed animal": "bunny", "rabbit": "bunny",
    "bunny rabbit": "bunny", "white bunny": "bunny", "stuffed rabbit": "bunny", "plushie": "bunny",
    "shoe": "shoes", "sneaker": "shoes", "sneakers": "shoes", "trainers": "shoes", "boots": "shoes",
    "boot": "shoes", "sandals": "shoes", "slippers": "shoes",
    "bag": "backpack", "school bag": "backpack", "bookbag": "backpack", "books": "book",
}
DETECT_CONF = 0.15
LOG_CONF = 0.25

# COCO keypoints
NOSE, L_SH, R_SH, L_EL, R_EL, L_WR, R_WR, L_HIP, R_HIP = 0, 5, 6, 7, 8, 9, 10, 11, 12
ARMS = {"left": (L_SH, L_EL, L_WR), "right": (R_SH, R_EL, R_WR)}
KP_CONF = 0.35

# The hat brim hangs over the bottom of the webcam view. Grey out that band on real camera frames so
# nothing is detected in the blur. Coordinates stay full-frame, so body.look_at still lines up.
MASK_BOTTOM = float(os.getenv("TEDDY_MASK_BOTTOM", 0))  # e.g. 0.4 if the brim droops again

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
            m.set_classes(PROMPTS)
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


UNSURE_WORDS = ("unsure", "not sure", "can't tell", "cannot tell", "hard to tell", "unclear", "can't see",
                "cannot see", "too blurry", "can't read", "cannot read", "unable to", "not clear", "i don't know")


def _unsure(ans):
    a = ans.lower()
    return len(a) < 3 or any(w in a for w in UNSURE_WORDS)


def _gemini(prompt, jpg_b64):
    key = os.getenv("GEMINI_API_KEY")
    if not key:
        _log("no GEMINI_API_KEY; can't fall back")
        return None
    try:
        r = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent",
            headers={"x-goog-api-key": key}, timeout=15, json={
                "contents": [{"parts": [{"inline_data": {"mime_type": "image/jpeg", "data": jpg_b64}},
                                        {"text": prompt}]}],
                "generationConfig": {"temperature": 0.2, "maxOutputTokens": 300},
            })
        r.raise_for_status()
        parts = r.json()["candidates"][0]["content"]["parts"]
        return " ".join(p.get("text", "") for p in parts if not p.get("thought")).strip() or None
    except Exception as e:
        _log("gemini error:", e)
        return None


def _save_boxed(img, det, path=SNAPSHOT):
    """Draw a big friendly box + label around det on a copy of img and save it as JPEG."""
    img = img.copy()
    h, w = img.shape[:2]
    x1, y1 = int((det["x"] - det["w"] / 2) * w), int((det["y"] - det["h"] / 2) * h)
    x2, y2 = int((det["x"] + det["w"] / 2) * w), int((det["y"] + det["h"] / 2) * h)
    th = max(3, w // 160)
    cv2.rectangle(img, (x1, y1), (x2, y2), (0, 200, 255), th)
    scale = max(0.8, w / 900)
    (tw, tht), _ = cv2.getTextSize(det["label"], cv2.FONT_HERSHEY_SIMPLEX, scale, th)
    ty = y1 - 10 if y1 - tht - 20 > 0 else y2 + tht + 10
    cv2.rectangle(img, (x1, ty - tht - 10), (x1 + tw + 16, ty + 8), (0, 200, 255), -1)
    cv2.putText(img, det["label"], (x1 + 8, ty), cv2.FONT_HERSHEY_SIMPLEX, scale, (40, 40, 40), th, cv2.LINE_AA)
    path.parent.mkdir(exist_ok=True)
    tmp = path.with_suffix(".tmp.jpg")
    cv2.imwrite(str(tmp), img, [cv2.IMWRITE_JPEG_QUALITY, 85])
    os.replace(tmp, path)  # atomic, so the server never serves a half-written file
    return path


def normalize_query(query):
    q = query.lower().strip().rstrip("?.!")
    for prefix in ("my ", "the ", "a ", "an ", "your ", "our "):
        if q.startswith(prefix):
            q = q[len(prefix):]
    return SYNONYMS.get(q, q)


# ---------- face emotion: YuNet face box -> HSEmotion (AffectNet, ONNX, CPU) ----------
MOODS = ["happy", "sad", "angry", "surprised", "neutral", "fearful"]
# HSEmotion 8 classes -> our 6 (contempt/disgust read as angry on a kid's face)
_HSE_TO_MOOD = [2, 2, 2, 5, 0, 4, 1, 3]  # Anger Contempt Disgust Fear Happiness Neutral Sadness Surprise
MOOD_WINDOW = 5.0


def _emotion_models():
    with _model_lock:
        if "emotion" not in _models:
            _models["emotion"] = None
            try:
                import onnxruntime as ort
                for name, url in [
                    ("face_detection_yunet_2023mar.onnx", "https://github.com/opencv/opencv_zoo/raw/main/models/"
                     "face_detection_yunet/face_detection_yunet_2023mar.onnx"),
                    ("enet_b0_8_best_afew.onnx", "https://github.com/HSE-asavchenko/face-emotion-recognition/raw/"
                     "main/models/affectnet_emotions/onnx/enet_b0_8_best_afew.onnx"),
                ]:
                    if not (MODELS / name).exists():
                        (MODELS / name).write_bytes(requests.get(url, timeout=120).content)
                det = cv2.FaceDetectorYN.create(str(MODELS / "face_detection_yunet_2023mar.onnx"), "", (320, 320), 0.7)
                sess = ort.InferenceSession(str(MODELS / "enet_b0_8_best_afew.onnx"),
                                            providers=["CPUExecutionProvider"])
                _models["emotion"] = (det, sess, threading.Lock())
            except Exception as e:
                _log("face mood disabled:", e)
        return _models["emotion"]


def _faces(img):
    """YuNet face boxes [(x, y, w, h)] in pixels, biggest first ([] if none or model unavailable)."""
    models = _emotion_models()
    if img is None or models is None:
        return []
    det, _, lock = models
    h, w = img.shape[:2]
    with lock:
        det.setInputSize((w, h))
        _, faces = det.detect(img)
    if faces is None:
        return []
    return sorted((tuple(f[:4]) for f in faces), key=lambda b: -b[2] * b[3])


def blur_faces_in(img):
    """Copy of img with every face heavily pixelated (padded box)."""
    out = img.copy()
    h, w = out.shape[:2]
    for fx, fy, fw, fh in _faces(img):
        pad = 0.25 * fw
        x1, y1 = int(max(0, fx - pad)), int(max(0, fy - pad))
        x2, y2 = int(min(w, fx + fw + pad)), int(min(h, fy + fh + 1.5 * pad))
        if x2 - x1 < 2 or y2 - y1 < 2:
            continue
        small = cv2.resize(out[y1:y2, x1:x2], (6, 6), interpolation=cv2.INTER_AREA)
        out[y1:y2, x1:x2] = cv2.resize(small, (x2 - x1, y2 - y1), interpolation=cv2.INTER_NEAREST)
    return out


def _face_emotion(img):
    """probs over MOODS for the biggest face in img, or None. Nothing is written to disk."""
    models = _emotion_models()
    if img is None or models is None:
        return None
    _, sess, lock = models
    faces = _faces(img)
    if not faces:
        return None
    h, w = img.shape[:2]
    fx, fy, fw, fh = faces[0]
    if fw < 36:  # too far away to read an expression
        return None
    pad = 0.15 * fw
    x1, y1 = int(max(0, fx - pad)), int(max(0, fy - pad))
    x2, y2 = int(min(w, fx + fw + pad)), int(min(h, fy + fh + pad))
    face = cv2.cvtColor(img[y1:y2, x1:x2], cv2.COLOR_BGR2RGB)
    x = cv2.resize(face, (224, 224)).astype(np.float32) / 255
    x = ((x - [0.485, 0.456, 0.406]) / [0.229, 0.224, 0.225]).transpose(2, 0, 1)[None].astype(np.float32)
    with lock:
        logits = sess.run(None, {"input": x})[0][0]
    p = np.exp(logits - logits.max())
    p /= p.sum()
    out = np.zeros(len(MOODS))
    for i, m in enumerate(_HSE_TO_MOOD):
        out[m] += p[i]
    return out


def find_c270(max_index=5):
    """Index of the Logitech C270. OpenCV can't read camera names, but the C270 tops out at
    1280x720 while the MacBook and iPhone cameras default to 1920x1080. Falls back to 0."""
    for i in range(max_index):
        cap = cv2.VideoCapture(i)
        if not cap.isOpened():
            continue
        size = (cap.get(cv2.CAP_PROP_FRAME_WIDTH), cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        cap.release()
        if size == (1280, 720):
            _log(f"C270 webcam is camera {i}")
            return i
    _log("C270 not found, using camera 0 (set TEDDY_CAM to override)")
    return 0


class Vision:
    def __init__(self, cam_index=None, mock=False, source=None, background=True, log_sightings=True,
                 fall_detection=None, emotions=True, on_mood=None):
        """fall_detection: off unless True or TEDDY_FALLS=1 (Teddy is a kid buddy, not an alarm).
        on_mood(label, conf, ts) is called whenever the smoothed face mood changes."""
        self.mock = mock
        self.fall_detection = (os.getenv("TEDDY_FALLS") == "1") if fall_detection is None else fall_detection
        self.on_mood = on_mood
        self._mood_hist = deque()  # (ts, probs[6]); probabilities only, never images
        self._mood_label = None
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
            if cam_index is None:
                cam_index = int(os.environ["TEDDY_CAM"]) if os.getenv("TEDDY_CAM") else find_c270()
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

        self._log_sighting = _sighting_logger() if log_sightings else None
        self._last_logged = {}
        if background:
            self._start(self._pose_loop)
            if emotions:
                self._start(self._mood_loop)
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
            if MASK_BOTTOM > 0:
                f[int(f.shape[0] * (1 - MASK_BOTTOM)):] = 127
            with self._lock:
                self._frame = f
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
            time.sleep(delay)
        cap.release()

    def _wait_for_frame(self, timeout=8):
        end = time.time() + timeout
        while self._frame is None and time.time() < end:
            time.sleep(0.05)
        if self._frame is None:
            _log("WARNING: no frames yet from", self.source)

    def frame(self, blur_faces=False):
        """Latest BGR frame (numpy array) or None. blur_faces=True pixelates every face first:
        use that for anything that gets saved or shown."""
        with self._lock:
            f = None if self._frame is None else self._frame.copy()
        return blur_faces_in(f) if blur_faces and f is not None else f

    def close(self):
        self._running = False

    # ---------- objects ----------
    def _run_world(self, img, classes=None):
        model, lock = _world()
        with lock:
            if classes:
                model.set_classes(classes)
            try:
                res = model.predict(img, conf=DETECT_CONF, device=_device(), verbose=False,
                                    agnostic_nms=True)[0]  # "stuffed animal" + "plush toy" = one box
            finally:
                if classes:
                    model.set_classes(PROMPTS)
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
        return self._to_dets(self._run_world(img), LABELS)

    def find(self, query, save=True):
        """Best match for a spoken thing ("my keys", "red mug") or None.
        With save=True the dict also has "image": path to a JPEG of the frame with the object boxed
        (always the same file, SNAPSHOT), for the iPad screen."""
        label = normalize_query(query)
        img = self.frame()
        if img is None:
            return None
        if label in TRACKED:
            hits = [d for d in self._to_dets(self._run_world(img), LABELS) if d["label"] == label]
        else:  # open vocabulary: ask YOLO-World for exactly this thing
            hits = self._to_dets(self._run_world(img, classes=[label]), [label])
        if not hits:
            return None
        hit = hits[0]
        if save:
            hit["image"] = str(_save_boxed(blur_faces_in(img), hit))  # never save a face
        return hit

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

    # ---------- VLM: local qwen2.5vl, Gemini if slow or unsure ----------
    def _ask_vlm(self, prompt, max_side=512, num_predict=60, deadline=VLM_DEADLINE):
        """Returns (answer, source). answer is None if both models failed or were unsure."""
        img = self.frame()
        if img is None:
            return None, "none"
        s = max_side / max(img.shape[:2])
        if s < 1:
            img = cv2.resize(img, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
        jpg = base64.b64encode(cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 85])[1].tobytes()).decode()
        t = time.time()
        try:
            r = requests.post(f"{OLLAMA_URL}/api/chat", timeout=deadline, json={
                "model": VLM, "stream": False, "keep_alive": "30m",
                "options": {"temperature": 0.2, "num_predict": num_predict},
                "messages": [{"role": "user", "content": prompt, "images": [jpg]}],
            })
            r.raise_for_status()
            ans = r.json()["message"]["content"].strip()
            if not _unsure(ans):
                return ans, "qwen"
            _log(f"qwen unsure ({ans!r}); asking Gemini")
        except requests.Timeout:
            _log(f"qwen slower than {deadline}s; asking Gemini")
        except Exception as e:
            _log("ollama error:", e)
        ans = _gemini(prompt, jpg)
        _log(f"vlm answered in {time.time() - t:.1f}s via gemini")
        return (None if ans is None or _unsure(ans) else ans), "gemini"

    def identify(self):
        """One short spoken sentence about what the person is showing the bear."""
        ans, _ = self._ask_vlm(
            "You are a gentle teddy bear talking to a child or an older adult. "
            "Look at what the person is holding up or showing you (or the main thing in view). "
            "Say what it is in ONE short, friendly sentence under 15 words. "
            "Plain words only, no lists, no markdown. "
            "If you really can't tell what it is, reply only: UNSURE", num_predict=40)
        return ans or "Hmm, I'm not sure what that is. Can you hold it a little closer?"

    def read_text(self):
        """A kid holds up a word or a page: returns it ready to read aloud, the words exactly as written
        and then the tricky ones sounded out ("el-e-phant. Elephant!"). Speak it with
        voice.speak(text, mood="reading") for the slow voice."""
        from senses.phonics import reading_script
        ans, _ = self._ask_vlm(
            "Transcribe the words a child is holding up in this image, EXACTLY as written, in reading order. "
            "Output only those words, nothing else: no description, no quotes, no markdown. "
            "If there are no readable words, or it is too blurry, reply only: UNSURE",
            max_side=896, num_predict=200)
        script = reading_script(ans) if ans else ""
        return script or "I can't see the words yet. Can you hold it a little closer and keep it still?"

    def warmup(self):
        """Load all models so the first real call is fast."""
        _world(), _pose()
        self.detect()
        requests.post(f"{OLLAMA_URL}/api/generate", json={"model": VLM, "keep_alive": "30m"}, timeout=120)

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
            if not self.fall_detection:
                pass
            elif not _solid_body(k, box, w, h):
                pass  # partial/blurry body: fine for gestures, too weak to call a fall either way
            elif _is_fallen_pose(k, box):
                self._fallen_since = self._fallen_since or t
            else:
                self._fallen_since = None
        elif self.fall_detection and self._lying_person(img, model):
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
            rh, rw = r.orig_shape
            for k, box in zip(r.keypoints.data.cpu().numpy(), r.boxes.xyxy.tolist()):
                if _solid_body(k, box, rw, rh) and not _is_fallen_pose(k, box):
                    return True  # upright once rotated = horizontal in the real frame
        return False

    def person_fallen(self, hold=2.0):
        """True if the closest person has looked horizontal / head-at-hip-level for `hold`+ seconds.
        Always False unless fall detection is switched on (see __init__)."""
        if not self.fall_detection:
            return False
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

    # ---------- face mood (privacy: face crops live only in memory, nothing is ever saved) ----------
    def _mood_loop(self, every=1.0):
        while self._running:
            t0 = time.time()
            try:
                probs = _face_emotion(self.frame())
                now = time.time()
                if probs is not None:
                    self._mood_hist.append((now, probs))
                while self._mood_hist and now - self._mood_hist[0][0] > MOOD_WINDOW:
                    self._mood_hist.popleft()
                m = self.mood()
                label = m and m["label"]
                if label != self._mood_label:
                    self._mood_label = label
                    if m and self.on_mood:
                        self.on_mood(m["label"], m["conf"], m["ts"])
            except Exception as e:
                _log("mood loop error:", e)
            time.sleep(max(0.05, every - (time.time() - t0)))

    def mood(self):
        """Smoothed face emotion over the last 5 s: {"label", "conf", "ts"} or None if no face lately.
        label is one of happy, sad, angry, surprised, neutral, fearful."""
        hist = list(self._mood_hist)
        if len(hist) < 2 or time.time() - hist[-1][0] > MOOD_WINDOW:
            return None
        avg = np.mean([p for _, p in hist], axis=0)
        i = int(np.argmax(avg))
        if avg[i] < 0.4:  # weak call: the model over-reads calm faces as angry, so say neutral
            i = MOODS.index("neutral")
        return {"label": MOODS[i], "conf": round(float(avg[i]), 2), "ts": round(hist[-1][0], 2)}

    def vitals(self):
        """Presage was dropped (no Python SDK), so there are no webcam vitals: always {}."""
        return {}


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


def _solid_body(k, box, w, h):
    """A real, clearly seen torso: both shoulders and hips plus 8+ confident keypoints, and a box
    that isn't the whole frame (empty walls/ceilings produce frame-sized ghosts with no keypoints)."""
    good = k[:, 2] > 0.5
    if good.sum() < 8 or not all(good[[L_SH, R_SH, L_HIP, R_HIP]]):
        return False
    return (box[2] - box[0]) * (box[3] - box[1]) < 0.85 * w * h


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
