"""No face ever leaves the bear: every frame the brain saves goes through here first.

- find photos are cropped around the found object (plus a little room for "by the laptop" context)
- any face OpenCV can spot is pixelated (frontal + profile cascades, generously padded)
- periodic room snapshots are OFF unless TEDDY_SIGHTING_FRAMES=1 (then they're blurred too)
Mood check-ins never touch frames at all (senses/ sends labels only).
"""
import os

import cv2
import numpy as np

SIGHTING_FRAMES = os.getenv("TEDDY_SIGHTING_FRAMES") == "1"
_CASCADES = None


def _cascades():
    global _CASCADES
    if _CASCADES is None:
        d = cv2.data.haarcascades
        _CASCADES = [cv2.CascadeClassifier(d + n) for n in
                     ("haarcascade_frontalface_default.xml", "haarcascade_frontalface_alt2.xml", "haarcascade_profileface.xml")]
    return _CASCADES


def faces(img):
    gray = cv2.equalizeHist(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY))
    h, w = gray.shape
    found = []
    for i, c in enumerate(_cascades()):
        for flip in ((False, True) if i == 2 else (False,)):  # profile cascade only sees one side; mirror it
            g = cv2.flip(gray, 1) if flip else gray
            for (x, y, fw, fh) in c.detectMultiScale(g, scaleFactor=1.1, minNeighbors=4, minSize=(max(20, w // 30),) * 2):
                found.append((w - x - fw if flip else x, y, fw, fh))
    return found


def blur_faces(img):
    """Pixelate every face. senses' YuNet detector first (best), OpenCV cascades as a backup pass."""
    try:
        from senses.vision import blur_faces_in
        img = blur_faces_in(img)
    except Exception:
        pass
    out = img.copy()
    h, w = out.shape[:2]
    for (x, y, fw, fh) in faces(img):
        px, py = int(fw * 0.4), int(fh * 0.4)
        x0, y0, x1, y1 = max(0, x - px), max(0, y - py), min(w, x + fw + px), min(h, y + fh + py)
        roi = out[y0:y1, x0:x1]
        if roi.size:
            small = cv2.resize(roi, (max(1, (x1 - x0) // 16), max(1, (y1 - y0) // 16)), interpolation=cv2.INTER_LINEAR)
            out[y0:y1, x0:x1] = cv2.resize(small, (x1 - x0, y1 - y0), interpolation=cv2.INTER_NEAREST)
    return out


def crop_around(img, x, y, bw=None, bh=None, context=2.6, min_frac=0.35):
    """Crop to the object (x, y = centre 0..1, bw/bh = box size 0..1) with some room around it."""
    h, w = img.shape[:2]
    bw, bh = bw or 0.15, bh or 0.15
    cw = min(1.0, max(min_frac, bw * context))
    ch = min(1.0, max(min_frac, bh * context))
    x0 = int(np.clip(x - cw / 2, 0, 1 - cw) * w)
    y0 = int(np.clip(y - ch / 2, 0, 1 - ch) * h)
    return img[y0:y0 + int(ch * h), x0:x0 + int(cw * w)]


def safe(img, box=None):
    """What we're allowed to keep: optional crop to the object, then faces pixelated."""
    if img is None:
        return None
    if box:
        img = crop_around(img, box["x"], box["y"], box.get("w"), box.get("h"))
    return blur_faces(img)
