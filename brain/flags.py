"""Sponsor feature flags. Every integration is optional: a missing key turns that piece off,
never the demo. `python -m brain.flags` prints what's on.

    Snowflake   SNOWFLAKE_ACCOUNT/USER/PASSWORD      brain, how-to + homework knowledge, parent insights
    Tiger Data  TIGER_DATABASE_URL (postgres://...)  time-series log of sessions, skills, finds, moods
    Solana      solana + spl-token CLIs, keypair     devnet "skill badge" per finished how-to
    ElevenLabs  ELEVENLABS_API_KEY                   Teddy's voice (browser speech otherwise)
    Gemini      GEMINI_API_KEY                       vision fallback (used by senses/)
    Vultr       VULTR_API_KEY or PARENT_HOST         where the Parent dashboard + API are deployed

TEDDY_AUDIENCE=kid (default) hides the emergency/caregiver features from the UI and prompts;
CPR still switches on by voice ("how do I do CPR", "someone's not breathing").
"""
import os
import shutil
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

AUDIENCE = os.getenv("TEDDY_AUDIENCE", "kid")
KID = AUDIENCE == "kid"
KID_NAME = os.getenv("TEDDY_KID", "Maya")
SOLANA_KEYPAIR = Path(os.getenv("SOLANA_KEYPAIR", ROOT / "data" / "solana" / "teddy-devnet.json"))


def _on(name):
    return os.getenv(f"TEDDY_DISABLE_{name.upper()}") != "1"


def snowflake():
    return _on("snowflake") and all(os.getenv(k) for k in ("SNOWFLAKE_ACCOUNT", "SNOWFLAKE_USER", "SNOWFLAKE_PASSWORD"))


def tiger():
    return _on("tiger") and bool(os.getenv("TIGER_DATABASE_URL"))


def solana():
    return _on("solana") and bool(shutil.which("spl-token")) and SOLANA_KEYPAIR.exists()


def elevenlabs():
    return _on("elevenlabs") and bool(os.getenv("ELEVENLABS_API_KEY"))


def gemini():
    return _on("gemini") and bool(os.getenv("GEMINI_API_KEY"))


def vultr():
    return bool(os.getenv("VULTR_API_KEY") or os.getenv("PARENT_HOST"))


def status():
    return {"audience": AUDIENCE, "snowflake": snowflake(), "tiger": tiger(), "solana": solana(),
            "elevenlabs": elevenlabs(), "gemini": gemini(), "vultr": vultr()}


if __name__ == "__main__":
    for k, v in status().items():
        print(f"  {k:11s} {v}")
