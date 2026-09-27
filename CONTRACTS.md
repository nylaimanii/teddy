# Servo map. 90 = neutral. Serial: "<id> <angle>\n"
0 head_pan, 1 head_tilt, 2 arm_l, 3 arm_r, 4 leg_l_side, 5 leg_l_kick, 6 leg_r_side, 7 leg_r_kick

# body/bear.py  (Agent A)
class Body(port=None, mock=False):
  move(joint, angle); pose(name)  # neutral, wave, think, happy, sad, alert, listen
  look_at(x, y)   # 0..1 camera coords -> head pan/tilt
  point_at(x, y)  # look_at + raise the closest arm
  dance(seconds=10); cpr_beat(bpm=110, seconds=30); stop()

# senses/  (Agent B)
class Vision(cam_index=0, mock=False):
  frame(); detect() -> [{"label","x","y","conf"}]; find(query) -> dict|None
  identify() -> str; read_text() -> str; person_fallen() -> bool
  gestures() -> {"type": wave|point|come_here|thumbs_up, "x","y"} | None
  vitals() -> {"heart_rate","breathing_rate"} or {}
  mood() -> {"label","conf","ts"} | None   # label: happy|sad|angry|surprised|neutral|fearful; labels only, no images
  background loop calls brain.snowflake.log_sighting(label, x, y)
voice.py: listen(seconds=5) -> str;  speak(text, mood="warm")

# brain/  (Agent C)
snowflake.py: log_sighting(label,x,y); last_seen(label); log_event(kind,data); ask(question, domain) -> str
agent.py = main loop. web/server.py = phone controls + caregiver dashboard.
