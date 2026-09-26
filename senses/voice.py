"""Teddy's ears and mouth. See CONTRACTS.md (senses/, Agent B).

    listen(seconds=5) -> str       # Whisper base on the Mac mic
    speak(text, mood="warm")       # ElevenLabs, falls back to macOS `say`

Mock mode (no mic/speaker): set TEDDY_MOCK=1 or call set_mock(True, audio_file=...).
In mock mode listen() transcribes the audio file if given, else reads a typed line;
speak() just prints.
"""
import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import numpy as np
import requests

ROOT = Path(__file__).resolve().parent.parent
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass

SAMPLE_RATE = 16000
WHISPER_SIZE = os.getenv("TEDDY_WHISPER", "base")
# George: warm, gentle storyteller. Override with ELEVENLABS_VOICE_ID in .env.
VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID", "JBFqnCBsd6RMkjVDRZzb")
SAY_VOICE = os.getenv("TEDDY_SAY_VOICE", "")  # e.g. "Samantha"; blank = system default

# mood -> (ElevenLabs stability, style, `say` words-per-minute)
MOODS = {
    "warm": (0.55, 0.35, 170),
    "happy": (0.35, 0.6, 190),
    "calm": (0.75, 0.15, 150),
    "sad": (0.7, 0.3, 145),
    "alert": (0.4, 0.5, 200),
    "urgent": (0.4, 0.5, 200),
}

_mock = {"on": os.getenv("TEDDY_MOCK") == "1", "audio_file": None, "text": None}
_whisper = None
_whisper_lock = threading.Lock()
speaking = threading.Event()  # set while Teddy talks, so the agent doesn't listen to itself


def _log(*a):
    print("[voice]", *a, flush=True)


def set_mock(on=True, audio_file=None, text=None):
    """Mock mode: listen() transcribes audio_file, else returns `text`, else asks on stdin."""
    _mock.update(on=on, audio_file=audio_file, text=text)


def _model():
    global _whisper
    with _whisper_lock:
        if _whisper is None:
            from faster_whisper import WhisperModel
            _whisper = WhisperModel(WHISPER_SIZE, device="cpu", compute_type="int8")
        return _whisper


def transcribe(audio):
    """audio: path to an audio file, or float32 mono numpy at 16 kHz."""
    segs, _ = _model().transcribe(audio, language="en", beam_size=1, vad_filter=True,
                                  initial_prompt="Hi Teddy.")
    return " ".join(s.text.strip() for s in segs).strip()


def listen(seconds=5):
    """Record `seconds` from the mic and return what was said ("" if nothing)."""
    if _mock["on"]:
        if _mock["audio_file"]:
            return transcribe(str(_mock["audio_file"]))
        if _mock["text"] is not None:
            return _mock["text"]
        try:
            return input("you> ").strip()
        except EOFError:
            return ""
    import sounddevice as sd
    audio = sd.rec(int(seconds * SAMPLE_RATE), samplerate=SAMPLE_RATE, channels=1, dtype="float32")
    sd.wait()
    audio = audio[:, 0]
    if np.abs(audio).max() < 0.01:  # silence, skip whisper
        return ""
    return transcribe(audio)


def _elevenlabs(text, mood):
    key = os.getenv("ELEVENLABS_API_KEY")
    if not key:
        return None
    stability, style, _ = MOODS.get(mood, MOODS["warm"])
    r = requests.post(
        f"https://api.elevenlabs.io/v1/text-to-speech/{VOICE_ID}?output_format=mp3_44100_128",
        headers={"xi-api-key": key, "Content-Type": "application/json"},
        json={"text": text, "model_id": "eleven_flash_v2_5",
              "voice_settings": {"stability": stability, "similarity_boost": 0.75, "style": style,
                                 "use_speaker_boost": True}},
        timeout=20,
    )
    r.raise_for_status()
    f = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
    f.write(r.content)
    f.close()
    return f.name


def speak(text, mood="warm"):
    """Say text out loud (blocks until done)."""
    text = (text or "").strip()
    if not text:
        return
    if _mock["on"]:
        print(f"teddy ({mood})> {text}", flush=True)
        return
    speaking.set()
    try:
        path = None
        try:
            path = _elevenlabs(text, mood)
        except Exception as e:
            _log("ElevenLabs failed, using say:", e)
        if path:
            subprocess.run(["afplay", path])
            os.unlink(path)
        else:
            cmd = ["say", "-r", str(MOODS.get(mood, MOODS["warm"])[2])]
            if SAY_VOICE:
                cmd += ["-v", SAY_VOICE]
            subprocess.run(cmd + [text])
    finally:
        speaking.clear()


if __name__ == "__main__":
    speak(" ".join(sys.argv[1:]) or "Hi! I'm Teddy. What's your name?")
