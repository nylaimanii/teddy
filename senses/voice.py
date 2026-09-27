"""Teddy's ears and mouth. See CONTRACTS.md (senses/, Agent B).

    listen(seconds=5) -> str               # Whisper base on the Mac mic
    speak(text, mood="warm") -> bytes      # ElevenLabs Matilda (mp3); macOS `say` (wav) if that fails
    wait_for_wake() -> str                 # blocks until "hey Teddy"; returns anything said after it

Audio goes to the iPad page if one is connected: the server calls
    voice.set_sink(fn)   # fn(audio_bytes, mime, text, mood) -> True if a page took it
If no sink is set, or it returns False, the Mac plays it.

Mock mode (no mic/speaker): set TEDDY_MOCK=1 or call set_mock(True, audio_file=...).
In mock mode listen() transcribes the audio file if given, else reads a typed line;
speak() just prints.
"""
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
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
VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID", "XrExE9yKIg1WjnnlVkGX")  # Matilda: warm, friendly
TTS_MODEL = "eleven_flash_v2_5"
SAY_VOICE = os.getenv("TEDDY_SAY_VOICE", "")  # e.g. "Samantha"; blank = system default

# mood -> (ElevenLabs stability, style, `say` words-per-minute[, ElevenLabs speed])
MOODS = {
    "reading": (0.8, 0.1, 115, 0.75),  # slow and clear, for reading words to a kid
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


def _record(max_seconds, stop_after_silence=1.0, level=0.015):
    """Record up to max_seconds; stop early once someone has spoken and then gone quiet."""
    import sounddevice as sd
    chunks, spoke, quiet = [], False, 0.0
    block = int(0.1 * SAMPLE_RATE)
    with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32", blocksize=block) as st:
        for _ in range(int(max_seconds / 0.1)):
            data, _ = st.read(block)
            chunks.append(data[:, 0].copy())
            loud = np.sqrt(np.mean(data ** 2)) > level
            spoke = spoke or loud
            quiet = 0.0 if loud else quiet + 0.1
            if spoke and quiet >= stop_after_silence:
                break
    audio = np.concatenate(chunks)
    return audio if spoke else None


def listen(seconds=5):
    """Record up to `seconds` from the mic (ends ~1 s after the speaker stops); returns the words or ""."""
    if _mock["on"]:
        if _mock["audio_file"]:
            return transcribe(str(_mock["audio_file"]))
        if _mock["text"] is not None:
            return _mock["text"]
        try:
            return input("you> ").strip()
        except EOFError:
            return ""
    audio = _record(seconds)
    return transcribe(audio) if audio is not None else ""


# "hey teddy" / "hi teddy" and the ways Whisper tends to spell it
WAKE_RE = re.compile(r"\b(?:hey|hi|hay|hei|okay|ok)[\s,.!-]*(?:teddy|teddie|tedi|tedy|tetty|teddi|ted e|freddy|eddie)\b[\s,.!?-]*",
                     re.I)


def heard_wake(text):
    """If text contains the wake phrase, return what came after it ("" if nothing); else None."""
    m = WAKE_RE.search(text or "")
    return None if m is None else text[m.end():].strip()


def wait_for_wake(timeout=None, level=0.015):
    """Block until someone says "hey Teddy" (or "hi Teddy"). Returns whatever they said after it in
    the same breath ("hey Teddy, where's my bunny?" -> "where's my bunny?"), or "" if just the wake
    phrase, or None on timeout. Ignores the mic while Teddy himself is speaking."""
    if _mock["on"]:
        text = _mock["text"] if _mock["text"] is not None else (
            transcribe(str(_mock["audio_file"])) if _mock["audio_file"] else input("you (say hey teddy)> "))
        rest = heard_wake(text)
        return rest if rest is not None else None
    import sounddevice as sd
    block = int(0.1 * SAMPLE_RATE)
    end = time.time() + timeout if timeout else None
    ring, voiced, quiet = [], 0, 0.0
    with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32", blocksize=block) as st:
        while end is None or time.time() < end:
            data, _ = st.read(block)
            if speaking.is_set():
                ring, voiced, quiet = [], 0, 0.0
                continue
            ring.append(data[:, 0].copy())
            ring = ring[-60:]  # keep at most 6 s
            loud = np.sqrt(np.mean(data ** 2)) > level
            voiced += loud
            quiet = 0.0 if loud else quiet + 0.1
            # transcribe each burst of speech once it pauses (or gets long), not every 100 ms
            if voiced >= 3 and (quiet >= 0.5 or len(ring) >= 60):
                text = _wake_transcribe(np.concatenate(ring))
                ring, voiced, quiet = [], 0, 0.0
                rest = heard_wake(text)
                if rest is not None:
                    _log(f"woke on {text!r}")
                    return rest
            elif voiced == 0 and len(ring) > 10:
                ring = ring[-10:]  # silence: keep 1 s of lead-in only
    return None


def _wake_transcribe(audio):
    segs, _ = _model().transcribe(audio, language="en", beam_size=1, vad_filter=True,
                                  hotwords="Hey Teddy", condition_on_previous_text=False)
    return " ".join(s.text.strip() for s in segs).strip()


_sink = None


def set_sink(fn):
    """fn(audio_bytes, mime, text, mood) -> bool. Return True if a page played it; False = Mac plays it."""
    global _sink
    _sink = fn


def _elevenlabs(text, mood):
    key = os.getenv("ELEVENLABS_API_KEY")
    if not key:
        return None
    stability, style, _, *speed = MOODS.get(mood, MOODS["warm"])
    settings = {"stability": stability, "similarity_boost": 0.75, "style": style, "use_speaker_boost": True}
    if speed:
        settings["speed"] = speed[0]
    r = requests.post(
        f"https://api.elevenlabs.io/v1/text-to-speech/{VOICE_ID}?output_format=mp3_44100_128",
        headers={"xi-api-key": key, "Content-Type": "application/json"},
        json={"text": text, "model_id": TTS_MODEL, "voice_settings": settings},
        timeout=20,
    )
    r.raise_for_status()
    return r.content


def _say_wav(text, mood):
    cmd = ["say", "-r", str(MOODS.get(mood, MOODS["warm"])[2]), "--data-format=LEI16@22050"]
    if SAY_VOICE:
        cmd += ["-v", SAY_VOICE]
    with tempfile.NamedTemporaryFile(suffix=".wav") as f:
        subprocess.run(cmd + ["-o", f.name, text], check=True)
        return Path(f.name).read_bytes()


def synthesize(text, mood="warm"):
    """-> (audio_bytes, mime). ElevenLabs mp3, or a `say` wav if ElevenLabs is unavailable."""
    try:
        audio = _elevenlabs(text, mood)
        if audio:
            return audio, "audio/mpeg"
    except Exception as e:
        _log("ElevenLabs failed, using say:", e)
    return _say_wav(text, mood), "audio/wav"


def _duration(audio, mime):
    if mime == "audio/wav":
        return max(0.0, (len(audio) - 44) / (22050 * 2))
    return len(audio) * 8 / 128000  # mp3 at 128 kbps


def _play_local(audio, mime):
    with tempfile.NamedTemporaryFile(suffix=".mp3" if mime == "audio/mpeg" else ".wav") as f:
        f.write(audio)
        f.flush()
        subprocess.run(["afplay", f.name])


def speak(text, mood="warm"):
    """Say text out loud and return the audio bytes. Blocks until it has finished playing
    (on the page or the Mac), so Teddy never listens to himself."""
    text = (text or "").strip()
    if not text:
        return b""
    if _mock["on"]:
        print(f"teddy ({mood})> {text}", flush=True)
        return b""
    audio, mime = synthesize(text, mood)
    speaking.set()
    try:
        sent = False
        if _sink:
            try:
                sent = bool(_sink(audio, mime, text, mood))
            except Exception as e:
                _log("page sink failed, playing on Mac:", e)
        if sent:
            time.sleep(_duration(audio, mime) + 0.3)  # page is playing it; stay "speaking" meanwhile
        else:
            _play_local(audio, mime)
    finally:
        speaking.clear()
    return audio


if __name__ == "__main__":
    speak(" ".join(sys.argv[1:]) or "Hi! I'm Teddy. What's your name?")
