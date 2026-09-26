"""Teddy's screen (the iPad page): what he's showing, and his voice.

The agent calls show(mode, **data) to change what's on screen, and speaks through ScreenVoice,
which sends each line to the iPad (ElevenLabs audio streamed by web/server.py, browser speech as
a fallback) and waits until the iPad says it finished playing. No iPad connected -> the Mac speaks.
"""
import threading
import uuid

_listeners = []
_state = {"type": "screen", "mode": "idle"}
_pending = {}  # speech id -> {"text", "mood", "done": Event}
_speakers = lambda: 0  # noqa: E731  (web/server.py replaces this with a live count)


def subscribe(fn):
    _listeners.append(fn)


def set_speaker_counter(fn):
    global _speakers
    _speakers = fn


def publish(msg):
    for fn in list(_listeners):
        try:
            fn(msg)
        except Exception as e:
            print(f"[screen] listener failed: {e}")


def show(mode, **data):
    """Switch the screen: idle, listen, think, find, story, homework, cpr, dance, read, identify, check, answer."""
    global _state
    _state = {"type": "screen", "mode": mode, **data}
    publish(_state)


def update(**data):
    """Change part of the current screen (e.g. next story page) without switching mode."""
    _state.update(data)
    publish(dict(_state))


def state():
    return dict(_state)


def speech(sid):
    return _pending.get(sid)


def spoken(sid):
    """The iPad finished playing speech `sid`."""
    p = _pending.get(sid)
    if p:
        p["done"].set()


class ScreenVoice:
    """voice-contract object: listen() from the Mac mic, speak() on the iPad."""

    def __init__(self, mic=None, fallback=None):
        self.mic = mic
        self.fallback = fallback

    def listen(self, seconds=5):
        return self.mic.listen(seconds) if self.mic else ""

    def speak(self, text, mood="warm"):
        if not text:
            return
        if _speakers() == 0:
            if self.fallback:
                return self.fallback.speak(text, mood=mood)
            return
        sid = uuid.uuid4().hex[:12]
        done = threading.Event()
        _pending[sid] = {"text": text, "mood": mood, "done": done}
        publish({"type": "say", "id": sid, "text": text, "mood": mood})
        done.wait(timeout=4 + len(text) / 11)  # ~11 chars/s of speech, plus network slack
        # keep the entry briefly so a late audio fetch still works
        threading.Timer(60, _pending.pop, args=(sid, None)).start()
