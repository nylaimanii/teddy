# Teddy (OwlHacks 2026)
AI teddy bear companion for people who are alone (kids home after school, seniors living alone).
Track: Human-Computer Interaction. Pitch: the interface IS a teddy bear. No screen, no app, just talk, point, and wave. Accessible to kids, seniors, and blind or low-vision users.
Top priority: win Best Use of Snowflake (Snowflake is the bear's brain + memory).
Hardware: Arduino Uno over USB serial /dev/cu.usbmodem11301 @115200. 8 SG90 servos on pins 2-9. Logitech C270 webcam in a hat on the head. The Mac runs everything.
Stack: Python 3, Ollama (qwen2.5vl:7b, qwen2.5:7b), YOLO-World + YOLO pose, Whisper, ElevenLabs, Presage, Snowflake Cortex, FastAPI, cloudflared.
3 Claude Code sessions (A, B, C) run in parallel. ONLY edit files in your own folders. Shared interfaces live in CONTRACTS.md; don't change them without telling the human.
Every module needs mock=True so it runs without hardware. Secrets go in .env (never commit). Commit only your own folders.
Demo-simple beats perfect.
