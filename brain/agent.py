"""Teddy's main loop: voice, gestures, and screen/phone buttons all flow into the same actions.

    python -m web.server             # the normal way: bear + iPad screen + caregiver dashboard
    python -m brain.agent            # bear only (voice on the Mac), real hardware where available
    python -m brain.agent --mock     # mock hardware; type to talk to Teddy (add --offline for a local brain)

(Run with -m from the repo root; `python brain/agent.py` would shadow the snowflake package.)
"""
import ast
import json
import operator
import os
import queue
import random
import re
import shutil
import sys
import threading
import time
from collections import deque
from fractions import Fraction

from brain import screen
from brain import snowflake as sf
from brain.mocks import MockBody, MockVision, MockVoice

INTENTS = ["find_object", "identify", "read", "homework", "cpr_coach", "first_aid",
           "fall_check", "chat", "dance", "mood_checkin", "story", "stop"]
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
    """Real camera; in mock mode, senses' real YOLO on a still image if TEDDY_MOCK_SOURCE is set."""
    if not mock or os.getenv("TEDDY_MOCK_SOURCE"):
        try:
            from senses.vision import Vision
            return Vision(mock=mock)
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
    if re.search(r"bleed|blood|burn|\bcut\b|chok|sting|\bbee\b|nosebleed|sprain|bump|scrape|poison|allergic", t):
        return {"intent": "first_aid", "question": text}
    if re.search(r"\bdanc", t):
        return {"intent": "dance"}
    if re.search(r"\bstory\b|\bstories\b|bedtime tale|once upon", t):
        return {"intent": "story", "question": text}
    m = _OBJ_RE.search(t)
    if m:
        return {"intent": "find_object", "object": m.group(1).strip()}
    if re.search(r"\bread (this|it|that|me|the)\b|what does (this|it) say", t):
        return {"intent": "read"}
    if re.search(r"what('s| is) (this|that)|what am i holding|what do you see|identify", t):
        return {"intent": "identify"}
    if math_expr(t) or re.search(r"homework|math|spell|science|history question", t):
        return {"intent": "homework", "question": text}
    if re.search(r"check on me|how do i look|am i okay", t):
        return {"intent": "fall_check"}
    return None


def llm_intent(text):
    prompt = (f"Classify what the person wants from their teddy bear robot. Intents: {', '.join(INTENTS)}.\n"
              "find_object = where is something; identify = what is this thing; read = read text aloud; "
              "homework = school question; cpr_coach = someone not breathing; first_aid = injury help; "
              "fall_check = they fell or want checking on; mood_checkin = they want to talk about feelings; "
              "story = tell a story; dance = dance; chat = anything else.\n"
              'Reply JSON: {"intent": "...", "object": "thing to find or null", "question": "the question or null"}\n'
              f"Person said: {text}")
    try:
        out = json.loads(sf.ollama(prompt, json_mode=True, temperature=0))
        if out.get("intent") in INTENTS:
            return out
    except Exception as e:
        print(f"[teddy] intent llm failed: {e}")
    return {"intent": "chat"}


# ------------------------------------------------------------------ exact math for homework
_WORDS = [(r"multiplied by|times|\bx\b|×", "*"), (r"divided by|÷|\bover\b", "/"), (r"\bplus\b|\badd\b", "+"),
          (r"\bminus\b|take away|subtract", "-"), (r"squared", "**2"), (r"cubed", "**3")]
_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.Pow: operator.pow, ast.USub: operator.neg}


def math_expr(text):
    """'what is 3/4 plus 1 half?' -> '3/4 + 1/2' if it's plain arithmetic, else None."""
    t = text.lower().replace("half", "1/2").replace("a quarter", "1/4")
    for pat, op in _WORDS:
        t = re.sub(pat, f" {op} ", t)
    m = re.search(r"[\d(][\d\s.+\-*/()]*[\d)]", t)
    if not m or not re.search(r"\d\s*[-+*/]\s*[\d(]|\*\*", m.group(0)):
        return None
    return re.sub(r"\s+", " ", m.group(0)).strip()


def math_eval(expr):
    """Exact answer with Fractions (so 1/2 + 1/4 = 3/4, not 0.75)."""
    def ev(n):
        if isinstance(n, ast.Expression):
            return ev(n.body)
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)):
            return Fraction(str(n.value))
        if isinstance(n, ast.BinOp) and type(n.op) in _OPS:
            if isinstance(n.op, ast.Pow) and abs(ev(n.right)) > 10:
                raise ValueError("too big")
            return _OPS[type(n.op)](ev(n.left), ev(n.right))
        if isinstance(n, ast.UnaryOp) and type(n.op) in _OPS:
            return _OPS[type(n.op)](ev(n.operand))
        raise ValueError("not arithmetic")
    try:
        v = ev(ast.parse(expr, mode="eval"))
    except Exception:
        return None
    return str(v.numerator) if v.denominator == 1 else f"{v.numerator}/{v.denominator}"


def _json_block(text):
    m = re.search(r"\{.*\}", text or "", re.S)
    return json.loads(m.group(0)) if m else None


def _plural(obj):
    return obj.endswith("s") and not obj.endswith("ss")


def _side(x):
    return "on the left" if x < 0.4 else "on the right" if x > 0.6 else "right in front of me"


# ------------------------------------------------------------------ the bear
class Teddy:
    def __init__(self, mock=False, body=None, vision=None, voice=None, wake_word=None, screen_voice=False):
        self.mock = mock
        self.body = body or load_body(mock)
        self.vision = vision or load_vision(mock)
        self.mic = voice or load_voice(mock)
        # On the iPad setup Teddy's voice plays on the screen; the Mac only listens.
        self.voice = screen.ScreenVoice(mic=self.mic, fallback=self.mic) if screen_voice else self.mic
        self.wake_word = (os.getenv("TEDDY_WAKE", "teddy") if wake_word is None else wake_word) \
            if not isinstance(self.mic, MockVoice) else ""
        if not isinstance(self.vision, MockVision) and hasattr(self.vision, "frame"):
            sf.set_frame_source(self.vision.frame)
        self.history = deque(maxlen=8)
        self.status = "idle"
        self.jobs = queue.Queue()
        self._answers = queue.Queue()
        self._awaiting = threading.Event()
        self._stop = threading.Event()
        self._voice_loop_on = False
        self._talking = False
        self._last_spoke = 0
        self._last_gesture = {}
        self._fall_cooldown = 0
        self._running = True
        threading.Thread(target=self._worker, daemon=True, name="teddy-actions").start()

    # ---- I/O helpers
    def say(self, text, mood="warm", pose=None):
        if pose:
            self._safe(self.body.pose, pose)
        sf.log_event("speak", {"reply": text, "mood": mood})
        self._talking = True
        speaking = getattr(self.mic, "speaking", None)  # senses.voice: tell the mic Teddy is talking
        if speaking is not None and self.voice is not self.mic:
            speaking.set()
        try:
            self.voice.speak(text, mood=mood)
        except Exception as e:
            print(f"[teddy] speak failed: {e}\n🧸 {text}")
        finally:
            if speaking is not None and self.voice is not self.mic:
                speaking.clear()
            self._talking = False
            self._last_spoke = time.time()
        return text

    def listen(self, seconds=6):
        """Get one answer from the person (routes through the voice loop if it's running)."""
        self._safe(self.body.pose, "listen")
        screen.publish({"type": "listening", "on": True})
        try:
            if self._voice_loop_on:
                while not self._answers.empty():
                    self._answers.get_nowait()
                self._awaiting.set()
                try:
                    wait = 60 if isinstance(self.mic, MockVoice) and self.mic.interactive else seconds + 6
                    text = self._answers.get(timeout=wait)
                except queue.Empty:
                    text = ""
                finally:
                    self._awaiting.clear()
            else:
                try:
                    text = self.mic.listen(seconds) or ""
                except Exception as e:
                    print(f"[teddy] listen failed: {e}")
                    text = ""
        finally:
            screen.publish({"type": "listening", "on": False})
        if text:
            sf.log_event("heard", {"text": text, "source": "voice"})
        return text

    def answer(self, text):
        """An answer typed/tapped on the screen while Teddy is waiting for one."""
        if self._awaiting.is_set():
            sf.log_event("heard", {"text": text, "source": "phone"})
            self._answers.put(text)
            return True
        return False

    def _safe(self, fn, *a, **kw):
        try:
            return fn(*a, **kw)
        except Exception as e:
            print(f"[teddy] {getattr(fn, '__name__', fn)} failed: {e}")

    def _body_wait(self, seconds):
        """Real Body queues motions and returns at once; wait for them, but let 'stop' cut in."""
        if not hasattr(self.body, "wait"):
            return
        end = time.time() + seconds + 3
        while time.time() < end and not self._stop.is_set():
            if self._safe(self.body.wait, 0.25):
                return

    def _while_saying(self, line, fn, *a, mood="warm"):
        """Say a filler line while a slow call (vision, LLM) runs. -> fn's result."""
        box = {}
        th = threading.Thread(target=lambda: box.update(r=self._safe(fn, *a)), daemon=True)
        th.start()
        self.say(line, mood=mood)
        th.join()
        return box.get("r")

    # ---- entry points (all three modalities end up in handle())
    def submit(self, intent, source="phone", **args):
        """Queue an action (non-blocking). 'stop' jumps the queue."""
        if intent == "stop":
            return self.handle("stop", source)
        self.jobs.put((intent, source, args))
        return {"queued": intent, "status": self.status}

    def hear(self, text, source="voice"):
        """Something was said (or typed on the screen) -> intent -> action."""
        text = (text or "").strip()
        if not text:
            return None
        if source != "voice" and self.answer(text):
            return {"answered": True}
        if self.wake_word and source == "voice" and time.time() - self._last_spoke > 12:
            if self.wake_word not in text.lower():
                return None
        if self.wake_word:
            text = re.sub(rf"^\W*(hey|hi|ok|okay)?\W*{self.wake_word}\W*", "", text, flags=re.I) or text
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
                if screen.state().get("mode") in ("think", "dance", "cpr", "listen"):
                    screen.show("idle")

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
        screen.hush()
        self._safe(self.body.stop)
        self._safe(self.body.pose, "neutral")
        screen.show("idle")
        return self.say("Okay, stopping.")

    def do_find_object(self, object=None, **_):
        obj = re.sub(r"^(my|the|a|an|our|your)\s+", "", (object or "").strip().lower())
        if not obj:
            self.say("What should I look for?")
            obj = self.listen()
            if not obj:
                return self.say("Okay, just ask me when you need something found.")
        it, they = ("them", "are") if _plural(obj) else ("it", "is")
        screen.show("find", object=obj, status="looking", caption=f"Looking for your {obj}…")
        # The camera rides in his hat: find() must see the room from the neutral head pose, because
        # point_at() maps camera x,y as if the head were centred.
        self._safe(self.body.pose, "neutral")
        self._body_wait(2)
        time.sleep(0.3)  # let the next camera frame catch up with the head
        hit = self._while_saying(f"Let me look for your {obj}!", self.vision.find, obj)
        if hit:
            x, y, w, h = hit["x"], hit["y"], hit.get("w"), hit.get("h")
            self._safe(self.body.point_at, x, y)  # head turns to look, that side's arm goes up
            frame = self._keep_frame(hit.get("image"), obj)
            sf.log_sighting(hit.get("label", obj), x, y, frame_path=frame, w=w, h=h)
            near = self._nearby_now(obj, x, y)
            line = f"Your {obj} {they} {_side(x)}" + (f", by the {near}!" if near else "!")
            screen.show("find", object=obj, status="found", frame=frame and f"/frames/{frame}",
                        box=None if hit.get("image") else {"x": x, "y": y, "w": w, "h": h},
                        when="just now", near=near, caption=line)
            return self.say(line, mood="happy")
        seen = sf.last_seen(obj)
        if seen:
            self._safe(self.body.look_at, seen["x"], seen["y"])
            place = f"by the {seen['near']}" if seen.get("near") else seen["where"]
            line = f"I don't see your {obj} right now, but I last saw {it} {seen['when']}, {place}."
            screen.show("find", object=obj, status="last_seen",
                        frame=seen.get("frame_path") and f"/frames/{seen['frame_path']}",
                        box={"x": seen["x"], "y": seen["y"], "w": seen.get("w"), "h": seen.get("h")},
                        when=seen["when"], near=seen.get("near"), caption=line)
            return self.say(line)
        screen.show("find", object=obj, status="not_found", caption=f"I haven't seen your {obj} yet.")
        return self.say(f"Hmm, I haven't seen your {obj} yet. Can you show me around the room?", pose="sad")

    def _keep_frame(self, image_path, tag):
        """Copy senses' boxed snapshot into data/frames (it's overwritten every find) -> file name."""
        if image_path and os.path.exists(image_path):
            name = f"{time.strftime('%Y%m%d_%H%M%S')}_{re.sub(r'[^a-z0-9]+', '-', tag)[:24]}_found.jpg"
            try:
                sf.FRAMES_DIR.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(image_path, sf.FRAMES_DIR / name)
                sf._q.put((sf.backend().upload_frame, sf.FRAMES_DIR / name))
                return name
            except OSError as e:
                print(f"[teddy] keep frame failed: {e}")
        return sf.save_frame(tag=tag, upload=True)

    def _nearby_now(self, obj, x, y):
        """Closest other thing in view right now ('laptop'), for 'by the laptop'."""
        dets = self._safe(self.vision.detect) or []
        others = [d for d in dets if d.get("label") not in (obj, "person") and obj not in d.get("label", "")]
        if not others:
            return None
        d = min(others, key=lambda d: (d["x"] - x) ** 2 + (d["y"] - y) ** 2)
        return d["label"] if ((d["x"] - x) ** 2 + (d["y"] - y) ** 2) ** .5 < 0.35 else None

    def do_identify(self, **_):
        screen.show("think", caption="Let me look…")
        self._safe(self.body.pose, "think")
        thing = self._while_saying("Ooh, let me look!", self.vision.identify) or ""
        if not thing:
            screen.show("idle")
            return self.say("I can't quite see it. Can you hold it closer?")
        thing = thing.strip().rstrip(".")
        screen.show("identify", thing=thing, frame=(f := sf.save_frame(tag="identify")) and f"/frames/{f}")
        return self.say(f"Ooh! I think that's {thing}." if len(thing.split()) < 8 else thing,
                        mood="happy", pose="happy")

    def do_read(self, **_):
        screen.show("think", caption="Hold it up for me…")
        self._safe(self.body.pose, "think")
        text = self._while_saying("Hold it up for me, I'll read it.", self.vision.read_text) or ""
        if not text.strip():
            screen.show("idle")
            return self.say("I can't make out the words. Can you hold it a little closer and still?")
        screen.show("read", text=text, frame=(f := sf.save_frame(tag="read")) and f"/frames/{f}")
        return self.say(text)

    def do_story(self, question=None, **_):
        topic = ""
        m = re.search(r"\babout\s+(.+)", question or "", re.I)
        if m:
            topic = m.group(1).strip(" ?.!")
        screen.show("think", caption="Thinking of a story…")
        self._safe(self.body.pose, "think")
        story = self._while_saying("Ooh, a story! Let me think of a good one.", make_story, topic)
        if not story:
            story = FALLBACK_STORY
        sf.log_event("story", {"title": story["title"], "topic": topic})
        screen.show("story", title=story["title"], pages=story["pages"], page=0)
        self._safe(self.body.pose, "happy")
        for i, page in enumerate(story["pages"]):
            if self._stop.is_set():
                return "stopped"
            screen.update(page=i)
            self.say(page["text"])
        if self._stop.is_set():
            return "stopped"
        screen.update(done=True)
        return self.say("The end! Did you like it?", mood="happy", pose="happy")

    def do_homework(self, question=None, **_):
        q = question
        if not q or q.lower().strip() in ("homework", "help with homework", "homework help"):
            screen.show("listen", caption="What's your question?")
            self.say("Ooh, homework! What's your question?")
            q = self.listen(8)
        if not q:
            return self.say("That's okay, ask me any time.")
        self._safe(self.body.pose, "think")
        expr = math_expr(q)
        truth = math_eval(expr) if expr else None
        if truth is not None:
            screen.show("think", caption="Let me work it out…")
            steps = self._while_saying("Let's work it out together!", math_steps, q, expr, truth)
            screen.show("homework", question=q, expr=expr, steps=steps, step=0, answer=truth)
            for i, st in enumerate(steps):
                if self._stop.is_set():
                    return "stopped"
                screen.update(step=i)
                self.say(st["say"])
            if self._stop.is_set():
                return "stopped"
            screen.update(done=True)
            return self.say("You're so smart! Want to try another one?", mood="happy", pose="happy")
        screen.show("think", caption="Let me check my books…")
        r = self._while_saying("Good question! Let me check my books.", sf.ask_detailed, q, "homework") or {}
        screen.show("answer", title="Homework", question=q, text=r.get("text", sf.NOT_SURE), source=r.get("source"))
        return self.say(r.get("answer", sf.NOT_SURE))

    def do_first_aid(self, question=None, **_):
        q = question or "basic first aid"
        self._safe(self.body.pose, "alert")
        screen.show("think", caption="Checking my first-aid guide…")
        r = sf.ask_detailed(q, "first_aid")
        ans = r["answer"] if r["answer"] != sf.NOT_SURE else "I'm not sure. If someone is badly hurt, call 911 right now."
        screen.show("answer", title="First aid", question=q, text=r.get("text") if r.get("source") else ans,
                    source=r.get("source"), urgent=True)
        return self.say(ans, mood="calm")

    def do_cpr_coach(self, **_):
        sf.log_event("alert", {"status": "cpr_started", "text": "CPR coach started"})
        self._safe(self.body.pose, "alert")
        screen.show("cpr", bpm=110, round=0, caption="Call 911 now")
        self.say("Call 911 now, and put them on speaker. Kneel beside them. Put the heel of your hand in the "
                 "middle of their chest, other hand on top. Push hard and fast with my arms. Ready? Go!",
                 mood="calm")
        for i in range(10):  # up to ~5 minutes, until someone says stop or help arrives
            if self._stop.is_set():
                break
            screen.update(round=i + 1, caption="Push hard and fast")
            self._safe(self.body.cpr_beat, 110, 30)  # queued; _body_wait below lets 'stop' cut in
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
        screen.show("check", status="fallen" if fallen else "checking", caption="Are you okay?")
        self.say(f"{sf.OWNER.split()[-1]}, are you okay? Say yes if you're okay." if fallen
                 else "I'm checking on you. Are you feeling okay?", mood="calm")
        reply = self.listen(8).lower()
        ok = bool(re.search(r"\b(yes|yeah|yep|fine|okay|ok|good|alright|all right)\b", reply)) \
            and not re.search(r"\b(not|help|hurt|pain|can't)\b", reply)
        if ok:
            sf.log_event("fall_check", {"status": "ok", "fallen": fallen, "text": reply, **vitals})
            screen.show("check", status="ok", caption="Glad you're okay!")
            return self.say("Phew! I'm glad you're okay. I'm right here if you need me.", pose="happy")
        status = "no_response" if not reply else "help_requested"
        if fallen:
            status = "fallen" if not reply else status
        sf.log_event("fall_check", {"status": status, "fallen": fallen, "text": reply, **vitals})
        sf.log_event("alert", {"status": status, "text": reply or "No answer to Teddy's check-in", **vitals})
        screen.show("check", status="alert", caption="Telling your family now")
        return self.say("I'm letting your family know right now. Stay still and stay warm. "
                        "If you're hurt badly, call 911.", mood="calm")

    def do_mood_checkin(self, **_):
        self._safe(self.body.pose, "listen")
        screen.show("mood", caption="How are you feeling today?")
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
        screen.show("mood", score=m.get("score"), mood=m.get("mood"), caption=reply)
        return self.say(sf._spoken(m.get("reply", "")) or "Thank you for telling me.",
                        pose="sad" if (m.get("score") or 3) <= 2 else "happy")

    def do_dance(self, **_):
        screen.show("dance")
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
        screen.show("idle", wave=True)
        return self.say(random.choice(HELLOS), mood="happy")

    def do_gesture_point(self, x=None, y=None, **_):
        if x is not None and y is not None:
            self._safe(self.body.look_at, x, y)
            self._body_wait(1)
        return self.do_identify()

    def do_gesture_come_here(self, **_):
        screen.show("listen", caption="I'm listening!")
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
        while self._running:
            t0 = time.time()
            try:
                text = self.mic.listen(5)
            except Exception as e:
                print(f"[teddy] listen failed: {e}")
                time.sleep(1)
                continue
            # Drop anything recorded while Teddy was talking (the iPad speaker is right next to the mic).
            if not text or self._talking or self._last_spoke > t0 - 0.3:
                continue
            if self._awaiting.is_set():
                self._answers.put(text)
            else:
                if not self.wake_word or self.wake_word in text.lower() or time.time() - self._last_spoke < 12:
                    sf.log_event("heard", {"text": text, "source": "voice"})
                self.hear(text, "voice")

    def _gesture_loop(self):
        while self._running:
            try:
                if not self._talking:
                    self.on_gesture(self.vision.gestures())
            except Exception as e:
                print(f"[teddy] gestures failed: {e}")
                time.sleep(1)
            time.sleep(0.25)

    def _fall_loop(self):
        while self._running:
            time.sleep(2)
            try:
                if time.time() > self._fall_cooldown and self.vision.person_fallen():
                    self._fall_cooldown = time.time() + 90
                    self.submit("fall_check", "vision")
            except Exception as e:
                print(f"[teddy] fall watch failed: {e}")
                time.sleep(5)

    def close(self):
        """Stop the loops and release hardware (serial port, mic)."""
        self._running = False
        self._stop.set()
        screen.hush()
        self._safe(self.body.stop)
        if hasattr(self.body, "close"):
            self._safe(self.body.close)
        try:
            import sounddevice as sd
            sd.stop()
        except Exception:
            pass
        if hasattr(self.vision, "close"):
            self._safe(self.vision.close)

    def start(self, voice=True):
        """Start the always-on loops (voice, gestures, fall watch). Returns immediately."""
        loops = [self._gesture_loop, self._fall_loop]
        if voice and not (isinstance(self.mic, MockVoice) and not self.mic.interactive):
            loops.append(self._voice_loop)
        for fn in loops:
            threading.Thread(target=fn, daemon=True, name=fn.__name__).start()
        self._safe(self.body.pose, "neutral")
        sf.log_event("boot", {"mock": self.mock, "brain": sf.backend().name})
        return self


# ------------------------------------------------------------------ story + homework generation (Cortex)
SCENES = ["day", "night", "sunset", "forest", "ocean", "snow", "space", "meadow", "castle", "rain"]

FALLBACK_STORY = {"title": "Teddy and the Sleepy Moon", "pages": [
    {"text": "Once upon a time, a little teddy bear looked up and saw the moon yawning.", "art": "🧸🌙😴", "scene": "night"},
    {"text": "Why are you so sleepy, Moon? asked Teddy. I've been shining all night, said the Moon.", "art": "🌙✨⭐", "scene": "night"},
    {"text": "So Teddy hummed a soft song, and all the stars twinkled along.", "art": "🧸🎵⭐⭐", "scene": "night"},
    {"text": "The Moon smiled and closed its eyes, and the Sun peeked up to say good morning.", "art": "🌙😊🌅", "scene": "sunset"},
    {"text": "And Teddy curled up for a cozy nap, happy that he helped a friend.", "art": "🧸💤💛", "scene": "meadow"},
]}


def make_story(topic=""):
    """A 5-page illustrated story from Cortex AI_COMPLETE (Ollama if Snowflake is down)."""
    prompt = (
        "Write a gentle, happy story for a young child, told by Teddy the teddy bear"
        + (f", about {topic}" if topic else "") + ". Exactly 5 pages. Each page is 1 or 2 short sentences "
        "with simple words. No scary parts.\nReturn ONLY JSON: {\"title\": \"...\", \"pages\": [{\"text\": \"...\", "
        "\"art\": \"3 to 5 emoji that illustrate this page\", \"scene\": \"one of " + ", ".join(SCENES) + "\"}]}")
    for gen in (sf.complete, lambda p: sf.ollama(p, json_mode=True, temperature=0.8)):
        try:
            s = _json_block(gen(prompt))
            pages = [{"text": sf._spoken(p["text"]), "art": p.get("art", "🧸"),
                      "scene": p.get("scene") if p.get("scene") in SCENES else "day"}
                     for p in s["pages"] if p.get("text")][:6]
            if len(pages) >= 3:
                return {"title": sf._spoken(s.get("title", "A Teddy Story")), "pages": pages}
        except Exception as e:
            print(f"[teddy] story generation failed: {e}")
    return None


def math_steps(question, expr, truth):
    """Worked steps [{show, say}] from Cortex; the final answer is always the exact Python one."""
    pretty = expr.replace("*", "×").replace("/", "÷") if "/" not in expr or "(" in expr else expr.replace("*", "×")
    prompt = (
        f"A child asked: {question}\nThe exact answer is {truth}. Explain how to solve {pretty} for a 7-year-old "
        "in 2 to 4 small steps. Each step has 'show' (a short math line, like '8 × 7 = 8 + 8 + ...') and 'say' "
        "(one short spoken sentence). The last step must show the answer "
        f"{truth}.\nReturn ONLY JSON: {{\"steps\": [{{\"show\": \"...\", \"say\": \"...\"}}]}}")
    try:
        s = _json_block(sf.complete(prompt))
        steps = [{"show": st["show"], "say": sf._spoken(st["say"])} for st in s["steps"] if st.get("say")][:5]
        if steps and truth in steps[-1]["show"].replace(" ", ""):
            return steps
        if steps:
            return steps[:-1] + [{"show": f"{pretty} = {truth}", "say": f"So the answer is {truth}!"}]
    except Exception as e:
        print(f"[teddy] math steps failed: {e}")
    return [{"show": pretty, "say": f"Let's figure out {pretty.replace('×', 'times').replace('÷', 'divided by')}."},
            {"show": f"{pretty} = {truth}", "say": f"The answer is {truth}!"}]


def main():
    mock = "--mock" in sys.argv or os.getenv("TEDDY_MOCK") == "1"
    sf.configure(mock=True if "--offline" in sys.argv or os.getenv("TEDDY_OFFLINE") == "1" else None)
    teddy = Teddy(mock=mock).start()
    teddy.say("Hi! I'm Teddy. Wave at me, or just say hi!", mood="happy")
    while True:
        time.sleep(1)


if __name__ == "__main__":
    main()
