"""Phone remote + caregiver dashboard. Runs the bear in-process.

    python -m web.server            # real hardware where available
    python -m web.server --mock     # no hardware, local memory
    then: python -m web.tunnel      # public URL + QR code for phones

Phone page:      http://localhost:8000/
Caregiver page:  http://localhost:8000/caregiver
"""
import os
import sys
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from brain import snowflake as sf
from brain.agent import INTENTS, Teddy

STATIC = Path(__file__).parent / "static"
MOCK = "--mock" in sys.argv or os.getenv("TEDDY_MOCK") == "1"

app = FastAPI(title="Teddy")
app.mount("/static", StaticFiles(directory=STATIC), name="static")
teddy: Teddy = None


@app.on_event("startup")
def _boot():
    global teddy
    sf.configure(mock=True if MOCK else None)
    teddy = Teddy(mock=MOCK).start(voice=os.getenv("TEDDY_VOICE", "1") == "1")


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


@app.get("/")
def phone():
    return FileResponse(STATIC / "index.html")


@app.get("/caregiver")
def caregiver():
    return FileResponse(STATIC / "caregiver.html")


@app.post("/api/action")
def action(a: Action):
    if a.intent not in INTENTS:
        raise HTTPException(400, f"unknown intent {a.intent}")
    args = {k: v for k, v in {"object": a.object, "question": a.question}.items() if v}
    teddy.submit(a.intent, "phone", **args)
    return {"ok": True, "intent": a.intent}


@app.post("/api/say")
def say(t: Text):
    return teddy.hear(t.text, "phone") or {"ignored": True}


@app.post("/api/gesture")
def gesture(g: Gesture):
    """Lets the demo fake a camera gesture from the phone."""
    return teddy.on_gesture(g.model_dump(), "phone") or {"ignored": True}


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
    return {"ok": True, "brain": b.name, "cortex_model": getattr(b, "model", None), "mock": MOCK}


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")
