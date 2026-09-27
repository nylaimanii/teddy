"""Teddy's screen (iPad) + caregiver dashboard. Runs the bear in-process.

    python -m web.server            # real hardware where available, Snowflake if .env has creds
    python -m web.server --mock     # no hardware (brain still uses Snowflake)
    python -m web.server --mock --offline   # no hardware, no internet: SQLite + Ollama brain
    python -m web.tunnel            # public URL + QR code for the iPad

Teddy's screen:  http://localhost:8000/  (or /teddy; tap "Wake Teddy" once so the iPad can play audio)
Caregiver:       http://localhost:8000/caregiver
Landing page:    http://localhost:8000/home
The same pages are deployed to Vercel (web/vercel-build.mjs) and call this API through the tunnel;
CORS allows *.vercel.app and localhost, plus anything in TEDDY_CORS_ORIGINS (comma-separated).

All of Teddy's speech plays on the screen: ElevenLabs audio is streamed through /api/tts/<id>;
the page falls back to the browser's own speech if that fails. No screen open -> the Mac speaks.
"""
import asyncio
import itertools
import json
import os
import re
import sys
import threading
import time
import uuid
from collections import deque
from pathlib import Path

import requests
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from brain import screen
from brain import snowflake as sf
from brain.agent import INTENTS, Teddy

STATIC = Path(__file__).parent / "static"
MOCK = "--mock" in sys.argv or os.getenv("TEDDY_MOCK") == "1"       # mock hardware
OFFLINE = "--offline" in sys.argv or os.getenv("TEDDY_OFFLINE") == "1"  # local brain instead of Snowflake
VOICE_ID = os.getenv("TEDDY_VOICE_ID", "XrExE9yKIg1WjnnlVkGX")  # Matilda
TTS_MODEL = "eleven_flash_v2_5"
MOODS = {"warm": (0.55, 0.35), "happy": (0.35, 0.6), "calm": (0.75, 0.15), "sad": (0.7, 0.3),
         "alert": (0.4, 0.5), "urgent": (0.4, 0.5)}  # stability, style (same feel as senses/voice.py)

app = FastAPI(title="Teddy")
app.mount("/static", StaticFiles(directory=STATIC), name="static")
app.mount("/demo", StaticFiles(directory=STATIC / "demo"), name="demo")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in os.getenv("TEDDY_CORS_ORIGINS", "").split(",") if o.strip()],
    allow_origin_regex=r"https://[\w.-]+\.vercel\.app|http://(localhost|127\.0\.0\.1)(:\d+)?",
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type"],
    max_age=600,
)
teddy: Teddy = None
_clients = {}  # client id -> (asyncio.Queue, is_speaker)
_loop = None
_audio = {}  # id -> (bytes, mime) handed over by senses.voice's sink
# Long-poll fallback: Cloudflare quick tunnels buffer event-streams, so the page polls /api/poll instead.
_log = deque(maxlen=400)  # (seq, json)
_seq = itertools.count(1)
_log_lock = threading.Lock()
_pollers = {}  # client id -> (last poll time, is_speaker)


def _broadcast(msg):
    """Thread-safe: push a message to every open screen (SSE queues + the long-poll log)."""
    data = json.dumps(msg, default=str)
    with _log_lock:
        _log.append((next(_seq), data))
    if _loop is None:
        return
    for q, _ in list(_clients.values()):
        _loop.call_soon_threadsafe(q.put_nowait, data)


def _speakers():
    now = time.time()
    return sum(1 for _, spk in _clients.values() if spk) + \
        sum(1 for t, spk in list(_pollers.values()) if spk and now - t < 30)


def _voice_sink(audio, mime, text, mood):
    """senses.voice.set_sink: anything that calls voice.speak() directly also plays on the screen."""
    if not _speakers():
        return False
    aid = uuid.uuid4().hex[:12]
    _audio[aid] = (audio, mime)
    while len(_audio) > 20:
        _audio.pop(next(iter(_audio)))
    _broadcast({"type": "say", "id": aid, "text": text, "mood": mood, "audio": f"/api/audio/{aid}"})
    return True


@app.on_event("startup")
def _boot():
    """Serve pages right away; the bear (camera, YOLO, Whisper, serial) wakes up in the background."""
    global _loop
    _loop = asyncio.get_event_loop()
    sf.configure(mock=True if OFFLINE else None)
    screen.set_speaker_counter(_speakers)
    screen.subscribe(_broadcast)
    sf.on_event(lambda ev: _broadcast({"type": "event", **ev}))
    screen.show("think", caption="Teddy is waking up…")
    threading.Thread(target=_wake, daemon=True, name="teddy-wake").start()


def _wake():
    global teddy
    t0 = time.time()
    try:
        t = Teddy(mock=MOCK, screen_voice=True).start(voice=os.getenv("TEDDY_VOICE", "1") == "1")
        if hasattr(t.mic, "set_sink"):
            t.mic.set_sink(_voice_sink)
        teddy = t
        print(f"[web] Teddy is awake ({time.time() - t0:.0f}s)", flush=True)
        screen.show("idle")
    except Exception as e:
        import traceback
        traceback.print_exc()
        screen.show("think", caption=f"Teddy couldn't wake up: {e}")


def _bear():
    if teddy is None:
        raise HTTPException(503, "Teddy is still waking up")
    return teddy


@app.on_event("shutdown")
def _sleep():
    """Let go of the serial port, camera and mic so the next run (or body/check.py) can have them."""
    if teddy:
        teddy.close()


# ------------------------------------------------------------------ pages
@app.get("/")
@app.get("/teddy")
def screen_page():
    return FileResponse(STATIC / "index.html")


@app.get("/home")
def landing():
    return FileResponse(STATIC / "landing.html")


@app.get("/teddy-api.js")
def teddy_api_js():
    return FileResponse(STATIC / "teddy-api.js", media_type="text/javascript")


@app.get("/config.js")
def config_js():
    """On the Mac the API is this same origin (""); Vercel's build writes its own config.js."""
    cfg = {"api": "", "video": os.getenv("TEDDY_VIDEO_URL", "")}
    return Response(f"window.TEDDY_CONFIG = {json.dumps(cfg)};\n", media_type="text/javascript",
                    headers={"Cache-Control": "no-store"})


@app.get("/caregiver")
def caregiver():
    return FileResponse(STATIC / "caregiver.html")


@app.get("/frames/{name}")
def frame(name: str):
    if not re.fullmatch(r"[\w.-]+\.jpg", name):
        raise HTTPException(404)
    path = sf.FRAMES_DIR / name
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "max-age=3600"})


# ------------------------------------------------------------------ live stream + voice
@app.get("/api/stream")
async def stream(request: Request, speaker: int = 0):
    """Server-sent events: screen changes, speech to play, and the live event feed."""
    cid = uuid.uuid4().hex
    q = asyncio.Queue()
    _clients[cid] = (q, bool(speaker))
    await q.put(json.dumps(screen.state()))
    for ev in sf.recent_events()[-15:]:
        await q.put(json.dumps({"type": "event", **ev}, default=str))

    async def gen():
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    data = await asyncio.wait_for(q.get(), timeout=15)
                    yield f"data: {data}\n\n"
                except asyncio.TimeoutError:
                    yield ": ping\n\n"  # keeps cloudflared + Safari from dropping the stream
        finally:
            _clients.pop(cid, None)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/poll")
async def poll(after: int = -1, speaker: int = 0, cid: str = ""):
    """Long-poll: waits up to 20 s for messages newer than `after`. after=-1 -> current screen + recent feed."""
    if cid:
        _pollers[cid] = (time.time(), bool(speaker))
    if after < 0:
        with _log_lock:
            last = _log[-1][0] if _log else 0
        msgs = [json.dumps(screen.state())] + [json.dumps({"type": "event", **ev}, default=str)
                                               for ev in sf.recent_events()[-15:]]
        return {"seq": last, "msgs": [json.loads(m) for m in msgs]}
    end = time.time() + 20
    while time.time() < end:
        with _log_lock:
            new = [(n, d) for n, d in _log if n > after]
        if new:
            if cid:
                _pollers[cid] = (time.time(), bool(speaker))
            return {"seq": new[-1][0], "msgs": [json.loads(d) for _, d in new]}
        await asyncio.sleep(0.1)
    if cid:
        _pollers[cid] = (time.time(), bool(speaker))
    return {"seq": after, "msgs": []}


@app.get("/api/tts/{sid}")
def tts(sid: str):
    """Stream ElevenLabs (Matilda, eleven_flash_v2_5) straight to the iPad's <audio>."""
    p = screen.speech(sid)
    key = os.getenv("ELEVENLABS_API_KEY")
    if not p or not key:
        raise HTTPException(404)  # page falls back to speechSynthesis
    stability, style = MOODS.get(p["mood"], MOODS["warm"])
    r = requests.post(
        f"https://api.elevenlabs.io/v1/text-to-speech/{VOICE_ID}/stream?output_format=mp3_44100_128",
        headers={"xi-api-key": key, "Content-Type": "application/json"},
        json={"text": p["text"], "model_id": TTS_MODEL,
              "voice_settings": {"stability": stability, "similarity_boost": 0.75, "style": style,
                                 "use_speaker_boost": True}},
        stream=True, timeout=20)
    if r.status_code != 200:
        print(f"[web] ElevenLabs {r.status_code}: {r.text[:200]}")
        raise HTTPException(502)
    return StreamingResponse(r.iter_content(4096), media_type="audio/mpeg", headers={"Cache-Control": "no-store"})


@app.get("/api/audio/{aid}")
def audio(aid: str):
    a = _audio.get(aid)
    if not a:
        raise HTTPException(404)
    return Response(a[0], media_type=a[1], headers={"Cache-Control": "no-store"})


@app.post("/api/spoken/{sid}")
def spoken(sid: str):
    screen.spoken(sid)
    return {"ok": True}


# ------------------------------------------------------------------ controls
class Action(BaseModel):
    intent: str
    object: str | None = None
    question: str | None = None


class Text(BaseModel):
    text: str


class Gesture(BaseModel):
    type: str
    x: float = 0.5
    y: float = 0.5


class Question(BaseModel):
    question: str
    domain: str | None = None


@app.post("/api/action")
def action(a: Action):
    if a.intent not in INTENTS:
        raise HTTPException(400, f"unknown intent {a.intent}")
    args = {k: v for k, v in {"object": a.object, "question": a.question}.items() if v}
    _bear().submit(a.intent, "phone", **args)
    return {"ok": True, "intent": a.intent}


@app.post("/api/say")
def say(t: Text):
    return _bear().hear(t.text, "phone") or {"ignored": True}


@app.post("/api/gesture")
def gesture(g: Gesture):
    """Lets the demo fake a camera gesture from the screen."""
    return _bear().on_gesture(g.model_dump(), "phone") or {"ignored": True}


@app.get("/api/state")
def state():
    return {"status": teddy.status if teddy else "booting", "screen": screen.state(), "speakers": _speakers()}


@app.get("/api/feed")
def feed(after: int = 0):
    return {"status": teddy.status if teddy else "booting", "events": sf.recent_events(after)}


@app.get("/api/stats")
def stats(days: int = 7):
    return sf.stats(days)


@app.post("/api/caregiver/ask")
def caregiver_ask(q: Question):
    return sf.caregiver_ask(q.question)


@app.post("/api/ask")
def ask(q: Question):
    return sf.ask_detailed(q.question, q.domain)


@app.get("/api/health")
def health():
    b = sf.backend()
    return {"ok": True, "awake": teddy is not None, "brain": b.name, "cortex_model": getattr(b, "model", None), "mock": MOCK,
            "screens": len(_clients), "speakers": _speakers()}


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")
    # Skip interpreter teardown: PortAudio's atexit hook can deadlock against a mic thread mid-recording,
    # leaving a zombie that holds the Arduino's serial port. Everything was released in _sleep().
    os._exit(0)
