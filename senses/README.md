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
v.detect(); v.find("my keys"); v.gestures(); v.person_fallen()
v.identify(); v.read_text(); v.vitals()
voice.listen(5); voice.speak("Hi!", mood="warm")   # moods: warm happy calm sad alert urgent
voice.set_mock(True, audio_file=None, text=None)   # or TEDDY_MOCK=1: listen() reads stdin, speak() prints
```

Notes
- Models auto-download into `senses/models/` (gitignored). Call `v.warmup()` at startup.
- Background thread logs sightings via `brain.snowflake.log_sighting`; falls back to `senses/local_sightings.json` if brain isn't ready.
- `gestures()` extras: `point` includes `side`; x,y for point is where the arm points, for others it's the hand.
- `detect()` also returns `person` plus `w`,`h` per box.
- `voice.speaking` is an Event set while Teddy talks (don't listen during it).
- Vitals: Presage SmartSpectra Node SDK via `presage/bridge.mjs` (`cd senses/presage && npm install`). Needs `PRESAGE_API_KEY` in `.env`.
  In mock mode without Presage, `vitals()` returns fake values flagged `"mock": True`.
