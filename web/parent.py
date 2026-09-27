"""Parent dashboard + API. Standalone (this is what runs on Vultr) and also mounted by web/server.py.

    uvicorn web.parent:app --host 0.0.0.0 --port 8080     # on the Vultr box (see web/deploy/)
    http://localhost:8000/parent                          # on the Mac, via web/server.py

It needs only the cloud stores: Snowflake (brain, knowledge, insights, photos) + Tiger Data (charts).
PARENT_PIN guards everything under /api/parent (send it as the X-Parent-Pin header); the page asks once.
Kid data never leaves these two databases; photos are face-blurred and served via short-lived signed URLs.
"""
import hmac
import os
import time
from pathlib import Path

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

from brain import flags, parent
from brain import snowflake as sf

STATIC = Path(__file__).parent / "static"
PIN = os.getenv("PARENT_PIN", "")
_cache = {}


def pin_ok(x_parent_pin: str = Header(default="")):
    if PIN and not hmac.compare_digest(x_parent_pin, PIN):
        time.sleep(0.5)  # slow down guessing
        raise HTTPException(401, "PIN needed")


def cached(key, fn, ttl=60):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    val = fn()
    _cache[key] = (time.time(), val)
    return val


router = APIRouter(prefix="/api/parent", dependencies=[Depends(pin_ok)])


class Q(BaseModel):
    question: str


@router.get("/week")
def week(days: int = 7):
    return cached(f"week{days}", lambda: parent.week(days), ttl=30)


@router.get("/insight")
def insight():
    return cached("insight", parent.insight, ttl=600)


@router.post("/ask")
def ask(q: Q):
    return parent.ask(q.question[:300])


@router.get("/check")
def check():
    return {"ok": True}


def page():
    return FileResponse(STATIC / "parent.html")


app = FastAPI(title="Teddy for parents")
app.add_middleware(CORSMiddleware, allow_origin_regex=r"https://[\w.-]+\.vercel\.app|http://(localhost|127\.0\.0\.1)(:\d+)?",
                   allow_methods=["GET", "POST"], allow_headers=["Content-Type", "X-Parent-Pin"])
app.include_router(router)
app.get("/")(page)
app.get("/parent")(page)


@app.get("/api/parent/health")
def health():
    return {"ok": True, "pin": bool(PIN), **flags.status(), "brain": sf.backend().name}


@app.on_event("startup")
def _boot():
    sf.configure()
