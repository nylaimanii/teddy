"""Teddy's main loop: voice, gestures, and phone buttons all flow into the same actions.

    python -m brain.agent            # real hardware where available, mocks for anything missing
    python -m brain.agent --mock     # everything mocked; type to talk to Teddy

(Run with -m from the repo root; `python brain/agent.py` would shadow the snowflake package.)
"""
import json
import os
import queue
import random
import re
import sys
import threading
import time
from collections import deque

from brain import snowflake as sf
from brain.mocks import MockBody, MockVision, MockVoice

INTENTS = ["find_object", "identify", "read", "homework", "cpr_coach", "first_aid",
           "fall_check", "chat", "dance", "mood_checkin", "stop"]
PORT = os.getenv("TEDDY_PORT")  # None -> Body auto-detects the Uno

PERSONA = (
    f"You are Teddy, a warm, gentle teddy bear who keeps {sf.OWNER} company. You speak out loud, so reply "
    "in one or two short sentences with simple words a six-year-old understands. Be cheerful, kind and "
    "patient. No emojis, lists or markdown. If someone sounds sad or lonely, comfort them and suggest calling "
    "someone they love. If anything sounds like an emergency, tell them to call 911 right away.")

HELLOS = ["Hi friend! I'm so happy to see you!", "Hello hello! I missed you!", "Hi there! Want to hang out?"]


# ------------------------------------------------------------------ loading real parts or mocks
def load_body(mock):
    if not mock:
        try:
            from body.bear import Body
            return Body(port=PORT)
        except Exception as e:
            print(f"[teddy] body unavailable ({e!r}), using mock")
    return MockBody()


def load_vision(mock):
    if not mock:
        try:
            from senses.vision import Vision
            return Vision()
        except Exception as e:
            print(f"[teddy] vision unavailable ({e!r}), using mock")
    return MockVision()


def load_voice(mock):
    if not mock:
        try:
            from senses import voice
            voice.listen, voice.speak  # noqa: B018 - make sure the contract is there
            return voice
        except Exception as e:
            print(f"[teddy] voice unavailable ({e!r}), using mock")
    return MockVoice()


# ------------------------------------------------------------------ intent
_OBJ_RE = re.compile(r"(?:find|where(?:'s| is| are| did i (?:leave|put))|lost|looking for|seen)\s+"
                     r"(?:my|the|a|an|our)?\s*([a-z][a-z ]{1,24}?)\s*(?:\?|$|\bgo\b|\bat\b|\.)")


def quick_intent(text):
    """Keyword router: instant, and never gets emergencies wrong."""
    t = text.lower()
    if re.search(r"\b(stop|quiet|shh|enough)\b", t):
        return {"intent": "stop"}
    if re.search(r"\bcpr\b|not breathing|no pulse|heart (stopped|attack)|chest compressions|collapsed|unconscious", t):
        return {"intent": "cpr_coach"}
    if re.search(r"\bi (fell|fall|have fallen)\b|fallen|can't get up|cannot get up", t):
        return {"intent": "fall_check"}
    if re.search(r"bleed|blood|burn|\bcut\b|chok|sting|bee|nosebleed|sprain|bump|scrape|poison|allergic", t):
        return {"intent": "first_aid", "question": text}
    if re.search(r"\bdanc", t):
        return {"intent": "dance"}
    m = _OBJ_RE.search(t)
    if m:
        return {"intent": "find_object", "object": m.group(1).strip()}
    if re.search(r"\bread (this|it|that|me|the)\b|what does (this|it) say", t):
        return {"intent": "read"}
    if re.search(r"what('s| is) (this|that)|what am i holding|what do you see|identify", t):
        return {"intent": "identify"}
    if re.search(r"homework|math|times|plus|minus|divided|spell|science|history question", t):
        return {"intent": "homework", "question": text}
    if re.search(r"check on me|how do i look|am i okay", t):
        return {"intent": "fall_check"}
    return None


def llm_intent(text):
    prompt = (f"Classify what the person wants from their teddy bear robot. Intents: {', '.join(INTENTS)}.\n"
              "find_object = where is something; identify = what is this thing; read = read text aloud; "
              "homework = school question; cpr_coach = someone not breathing; first_aid = injury help; "
              "fall_check = they fell or want checking on; mood_checkin = they want to talk about feelings; "
              "dance = dance; chat = anything else.\n"
              'Reply JSON: {"intent": "...", "object": "thing to find or null", "question": "the question or null"}\n'
              f"Person said: {text}")
    try:
        out = json.loads(sf.ollama(prompt, json_mode=True, temperature=0))
        if out.get("intent") in INTENTS:
            return out
    except Exception as e:
        print(f"[teddy] intent llm failed: {e}")
    return {"intent": "chat"}


# ------------------------------------------------------------------ the bear
class Teddy:
    def __init__(self, mock=False, body=None, vision=None, voice=None, wake_word=None):
        self.mock = mock
        self.body = body or load_body(mock)
        self.vision = vision or load_vision(mock)
        self.voice = voice or load_voice(mock)
        self.wake_word = (os.getenv("TEDDY_WAKE", "teddy") if wake_word is None else wake_word) \
            if not isinstance(self.voice, MockVoice) else ""
        self.history = deque(maxlen=8)
        self.status = "idle"
        self.jobs = queue.Queue()
        self._answers = queue.Queue()
        self._awaiting = threading.Event()
        self._stop = threading.Event()
        self._voice_loop_on = False
        self._last_spoke = 0
        self._last_gesture = {}
        self._fall_cooldown = 0
        threading.Thread(target=self._worker, daemon=True, name="teddy-actions").start()

    # ---- I/O helpers
    def say(self, text, mood="warm", pose=None):
        if pose:
            self._safe(self.body.pose, pose)
        sf.log_event("speak", {"reply": text, "mood": mood})
        try:
            self.voice.speak(text, mood=mood)
        except Exception as e:
            print(f"[teddy] speak failed: {e}\n🧸 {text}")
        self._last_spoke = time.time()
        return text

    def listen(self, seconds=6):
        """Get one answer from the person (routes through the voice loop if it's running)."""
        self._safe(self.body.pose, "listen")
        if self._voice_loop_on:
            while not self._answers.empty():
                self._answers.get_nowait()
            self._awaiting.set()
            try:
                wait = 60 if isinstance(self.voice, MockVoice) and self.voice.interactive else seconds + 4
                text = self._answers.get(timeout=wait)
            except queue.Empty:
                text = ""
            finally:
                self._awaiting.clear()
        else:
            try:
                text = self.voice.listen(seconds) or ""
            except Exception as e:
                print(f"[teddy] listen failed: {e}")
                text = ""
        if text:
            sf.log_event("heard", {"text": text, "source": "voice"})
        return text

    def _body_wait(self, seconds):
        """Real Body queues motions and returns at once; wait for them, but let 'stop' cut in."""
        if not hasattr(self.body, "wait"):
            return
        end = time.time() + seconds + 3
        while time.time() < end and not self._stop.is_set():
            if self._safe(self.body.wait, 0.25):
                return

    def _safe(self, fn, *a, **kw):
        try:
            return fn(*a, **kw)
        except Exception as e:
            print(f"[teddy] {getattr(fn, '__name__', fn)} failed: {e}")

    # ---- entry points (all three modalities end up in handle())
    def submit(self, intent, source="phone", **args):
        """Queue an action (non-blocking). 'stop' jumps the queue."""
        if intent == "stop":
            return self.handle("stop", source)
        self.jobs.put((intent, source, args))
        return {"queued": intent, "status": self.status}

    def hear(self, text, source="voice"):
        """Something was said (or typed on the phone) -> intent -> action."""
        text = (text or "").strip()
        if not text:
            return None
        if self.wake_word and source == "voice" and time.time() - self._last_spoke > 12:
            if self.wake_word not in text.lower():
                return None
        text = re.sub(rf"^\W*(hey|hi|ok|okay)?\W*{self.wake_word}\W*", "", text, flags=re.I) if self.wake_word else text
        if source != "voice":
            sf.log_event("heard", {"text": text, "source": source})
        it = quick_intent(text) or llm_intent(text)
        it.setdefault("question", text)
        if it["intent"] == "chat":
            it["text"] = text
        intent = it.pop("intent")
        return self.submit(intent, source, **{k: v for k, v in it.items() if v})

    def on_gesture(self, g, source="gesture"):
        if not g:
            return None
        kind = g.get("type")
        now = time.time()
        if now - self._last_gesture.get(kind, 0) < 4:
            return None
        self._last_gesture[kind] = now
        sf.log_event("gesture", {"type": kind, "x": g.get("x"), "y": g.get("y"), "source": source})
        return self.submit(f"gesture_{kind}", source, x=g.get("x"), y=g.get("y"))

    # ---- the worker that actually does things, one at a time
    def _worker(self):
        while True:
            intent, source, args = self.jobs.get()
            try:
                self.handle(intent, source, **args)
            except Exception as e:
                print(f"[teddy] {intent} crashed: {e!r}")
                self.say("Oops, my stuffing got tangled. Can you try again?", mood="sad")
            finally:
                self.status = "idle"

    def handle(self, intent, source="phone", **args):
        fn = getattr(self, f"do_{intent}", None)
        if fn is None:
            return self.say("I'm not sure how to do that yet.")
        if not intent.startswith("gesture_"):
            sf.log_event("intent", {"intent": intent, "source": source,
                                    **{k: v for k, v in args.items() if k in ("object", "question")}})
        if intent != "stop":
            self._stop.clear()
            self.status = intent
        return fn(**args)

    # ---- actions
    def do_stop(self, **_):
        self._stop.set()
        self._safe(self.body.stop)
        self._safe(self.body.pose, "neutral")
        return self.say("Okay, stopping.")

    def do_find_object(self, object=None, **_):
        obj = (object or "").strip()
        if not obj:
            self.say("What should I look for?")
            obj = self.listen()
            if not obj:
                return self.say("Okay, just ask me when you need something found.")
        self._safe(self.body.pose, "think")
        hit = self._safe(self.vision.find, obj)
        if hit:
            self._safe(self.body.point_at, hit["x"], hit["y"])
            sf.log_sighting(hit.get("label", obj), hit["x"], hit["y"])
            return self.say(f"I see your {obj}! It's right over there, {sf.describe_spot(hit['x'], hit['y'])}.",
                            mood="happy", pose=None)
        seen = sf.last_seen(obj)
        if seen:
            self._safe(self.body.look_at, seen["x"], seen["y"])
            return self.say(f"I can't see your {obj} right now, but I last saw it {seen['when']}, "
                            f"{seen['where']}.")
        return self.say(f"Hmm, I haven't seen your {obj}. Can you show me the room?", pose="sad")

    def do_identify(self, **_):
        self._safe(self.body.pose, "think")
        thing = self._safe(self.vision.identify) or ""
        if not thing:
            return self.say("I can't quite see it. Can you hold it closer?")
        thing = thing.strip().rstrip(".")
        return self.say(f"Ooh! I think that's {thing}.", mood="happy", pose="happy")

    def do_read(self, **_):
        self.say("Hold it up for me, I'll read it.")
        self._safe(self.body.pose, "think")
        text = self._safe(self.vision.read_text) or ""
        if not text.strip():
            return self.say("I can't make out the words. Can you hold it a little closer and still?")
        return self.say(text)

    def do_homework(self, question=None, **_):
        q = question
        if not q or q.lower().strip() in ("homework", "help with homework", "homework help"):
            self.say("Ooh, homework! What's your question?")
            q = self.listen(8)
        if not q:
            return self.say("That's okay, ask me any time.")
        self._safe(self.body.pose, "think")
        return self.say(sf.ask(q, "homework"))

    def do_first_aid(self, question=None, **_):
        q = question or "basic first aid"
        self._safe(self.body.pose, "alert")
        ans = sf.ask(q, "first_aid")
        if ans == sf.NOT_SURE:
            ans = "I'm not sure. If someone is badly hurt, call 911 right now."
        return self.say(ans, mood="calm")

    def do_cpr_coach(self, **_):
        sf.log_event("alert", {"status": "cpr_started", "text": "CPR coach started"})
        self._safe(self.body.pose, "alert")
        self.say("Call 911 now, and put them on speaker. Kneel beside them. Put the heel of your hand in the "
                 "middle of their chest, other hand on top. Push hard and fast with my arms. Ready? Go!",
                 mood="calm")
        for i in range(10):  # up to ~5 minutes, until someone says stop or help arrives
            if self._stop.is_set():
                break
            self._safe(self.body.cpr_beat, 110, 30)
            self._body_wait(30)
            if self._stop.is_set():
                break
            self.say(random.choice(["You're doing great. Keep pushing, hard and fast.",
                                    "Keep going! Help is on the way.",
                                    "Don't stop. Let the chest come all the way up."]), mood="calm")
        self._safe(self.body.pose, "neutral")
        return "cpr done"

    def do_fall_check(self, **_):
        self._safe(self.body.pose, "alert")
        fallen = bool(self._safe(self.vision.person_fallen))
        vitals = self._safe(self.vision.vitals) or {}
        if vitals:
            sf.log_event("vitals", vitals)
        self.say(f"{sf.OWNER.split()[-1]}, are you okay? Say yes if you're okay." if fallen
                 else "I'm checking on you. Are you feeling okay?", mood="calm")
        reply = self.listen(8).lower()
        ok = bool(re.search(r"\b(yes|yeah|yep|fine|okay|ok|good|alright|all right)\b", reply)) \
            and not re.search(r"\b(not|help|hurt|pain|can't)\b", reply)
        if ok:
            sf.log_event("fall_check", {"status": "ok", "fallen": fallen, "text": reply, **vitals})
            return self.say("Phew! I'm glad you're okay. I'm right here if you need me.", pose="happy")
        status = "no_response" if not reply else "help_requested"
        if fallen:
            status = "fallen" if not reply else status
        sf.log_event("fall_check", {"status": status, "fallen": fallen, "text": reply, **vitals})
        sf.log_event("alert", {"status": status, "text": reply or "No answer to Teddy's check-in", **vitals})
        return self.say("I'm letting your family know right now. Stay still and stay warm. "
                        "If you're hurt badly, call 911.", mood="calm")

    def do_mood_checkin(self, **_):
        self._safe(self.body.pose, "listen")
        self.say("How are you feeling today?")
        reply = self.listen(8)
        if not reply:
            sf.log_event("mood", {"score": None, "mood": "no answer"})
            return self.say("That's okay. I'm here whenever you want to talk.")
        try:
            m = json.loads(sf.ollama(
                'Rate this person\'s mood. Reply JSON {"score": 1-5 (1 very bad, 5 great), '
                '"mood": one of happy, okay, tired, sad, lonely, anxious, in pain, '
                '"reply": one or two warm sentences from a teddy bear}. '
                f"They said: {reply}", system=PERSONA, json_mode=True, temperature=0.3))
        except Exception:
            m = {"score": 3, "mood": "okay", "reply": "Thank you for telling me. I'm always here for you."}
        sf.log_event("mood", {"score": m.get("score"), "mood": m.get("mood"), "text": reply})
        if (m.get("score") or 3) <= 2:
            sf.log_event("alert", {"status": "low_mood", "text": reply, "mood": m.get("mood")})
        return self.say(sf._spoken(m.get("reply", "")) or "Thank you for telling me.",
                        pose="sad" if (m.get("score") or 3) <= 2 else "happy")

    def do_dance(self, **_):
        self.say("Dance party! Let's go!", mood="happy", pose="happy")
        self._safe(self.body.dance, 10)
        self._body_wait(10)
        self._safe(self.body.pose, "neutral")
        return self.say("Whew! That was fun!", mood="happy")

    def do_chat(self, text=None, question=None, **_):
        text = text or question or ""
        self.history.append(f"Person: {text}")
        try:
            reply = sf._spoken(sf.ollama("\n".join(self.history) + "\nTeddy:", system=PERSONA, temperature=0.7))
        except Exception as e:
            print(f"[teddy] chat failed: {e}")
            reply = "I love talking with you! Tell me more."
        self.history.append(f"Teddy: {reply}")
        return self.say(reply)

    # gestures
    def do_gesture_wave(self, **_):
        self._safe(self.body.pose, "wave")
        return self.say(random.choice(HELLOS), mood="happy")

    def do_gesture_point(self, x=None, y=None, **_):
        if x is not None and y is not None:
            self._safe(self.body.look_at, x, y)
        return self.do_identify()

    def do_gesture_come_here(self, **_):
        self.say("I'm listening!")
        text = self.listen(6)
        if not text:
            return self.say("I'm right here whenever you need me.")
        it = quick_intent(text) or llm_intent(text)
        it.setdefault("question", text)
        if it["intent"] == "chat":
            it["text"] = text
        return self.handle(it.pop("intent"), "gesture", **{k: v for k, v in it.items() if v})

    def do_gesture_thumbs_up(self, **_):
        return self.say("Yay! High five!", mood="happy", pose="happy")

    # ---- background loops
    def _voice_loop(self):
        self._voice_loop_on = True
        while True:
            try:
                text = self.voice.listen(5)
            except Exception as e:
                print(f"[teddy] listen failed: {e}")
                time.sleep(1)
                continue
            if not text:
                continue
            if self._awaiting.is_set():
                self._answers.put(text)
            else:
                if not self.wake_word or self.wake_word in text.lower() or time.time() - self._last_spoke < 12:
                    sf.log_event("heard", {"text": text, "source": "voice"})
                self.hear(text, "voice")

    def _gesture_loop(self):
        while True:
            try:
                self.on_gesture(self.vision.gestures())
            except Exception as e:
                print(f"[teddy] gestures failed: {e}")
                time.sleep(1)
            time.sleep(0.25)

    def _fall_loop(self):
        while True:
            time.sleep(2)
            try:
                if time.time() > self._fall_cooldown and self.vision.person_fallen():
                    self._fall_cooldown = time.time() + 90
                    self.submit("fall_check", "vision")
            except Exception as e:
                print(f"[teddy] fall watch failed: {e}")
                time.sleep(5)

    def start(self, voice=True):
        """Start the always-on loops (voice, gestures, fall watch). Returns immediately."""
        loops = [self._gesture_loop, self._fall_loop]
        if voice and not (isinstance(self.voice, MockVoice) and not self.voice.interactive):
            loops.append(self._voice_loop)
        for fn in loops:
            threading.Thread(target=fn, daemon=True, name=fn.__name__).start()
        self._safe(self.body.pose, "neutral")
        sf.log_event("boot", {"mock": self.mock, "brain": sf.backend().name})
        return self


def main():
    mock = "--mock" in sys.argv or os.getenv("TEDDY_MOCK") == "1"
    sf.configure(mock=True if mock else None)
    teddy = Teddy(mock=mock).start()
    teddy.say("Hi! I'm Teddy. Wave at me, or just say hi!", mood="happy")
    while True:
        time.sleep(1)


if __name__ == "__main__":
    main()
