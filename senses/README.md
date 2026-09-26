# senses/ (Agent B): Teddy's eyes, ears, mouth

```bash
uv pip install --python .venv/bin/python -r senses/requirements.txt
.venv/bin/python -m senses.demo        # mock tests, no hardware (uses senses/assets/)
.venv/bin/python -m senses.demo live   # real webcam + mic + speaker
```

```python
from senses.vision import Vision
from senses import voice
v = Vision()                                   # TEDDY_CAM=<index> picks the webcam
v = Vision(mock=True, source="some.jpg|.mp4")  # no hardware
v.detect(); v.gestures(); v.person_fallen()
v.find("my keys")   # -> {"label","x","y","conf","w","h","image": "senses/snapshots/find.jpg"} or None
v.identify(); v.read_text()   # qwen2.5vl locally; Gemini if qwen takes >6s or says it's unsure
v.vitals()          # always {} (Presage dropped)
voice.listen(5)
audio = voice.speak("Hi!", mood="warm")   # -> mp3 bytes (Matilda). moods: warm happy calm sad alert urgent
voice.set_sink(fn)  # fn(audio_bytes, mime, text, mood) -> True if the iPad page played it; else the Mac plays it
voice.set_mock(True, audio_file=None, text=None)   # or TEDDY_MOCK=1: listen() reads stdin, speak() prints
```

Notes
- Models auto-download into `senses/models/` (gitignored). Call `v.warmup()` at startup.
- Background thread logs sightings via `brain.snowflake.log_sighting`; falls back to `senses/local_sightings.json` if brain isn't ready.
- `gestures()` extras: `point` includes `side`; x,y for point is where the arm points, for others it's the hand.
- `detect()` also returns `person` plus `w`,`h` per box.
- `voice.speaking` is an Event set while Teddy talks (don't listen during it).
- `find()` overwrites the same snapshot file each call (atomic write), so serve it with no-cache.
- Env overrides: `TEDDY_CAM`, `TEDDY_GEMINI` (default gemini-flash-latest), `TEDDY_VLM_DEADLINE` (6), `ELEVENLABS_VOICE_ID`.
