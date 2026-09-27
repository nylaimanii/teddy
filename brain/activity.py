"""One call to record what the kid did: Snowflake EVENTS always, Tiger Data (time-series) when enabled.

    activity.log("skill_done", skill="Tying shoes", duration_s=184, detail={"guide": "tie-shoes"})

kinds: session_start, session_end, howto_start, howto_step, skill_done, homework_try, homework_done,
       find, read, badge
"""
import threading

from brain import flags
from brain import snowflake as sf

_state = {"activity": None}  # what Teddy is helping with right now (mood check-ins record it)


def log(kind, skill=None, duration_s=None, **detail):
    data = {k: v for k, v in {"skill": skill, "duration_s": duration_s, **detail}.items() if v is not None}
    sf.log_event(kind, data)
    if flags.tiger():
        from brain import tiger
        threading.Thread(target=tiger.log_activity, args=(kind, skill, duration_s, detail), daemon=True).start()


def set_current(what):
    """e.g. 'Tying shoes', 'Homework: 7 x 8', None when idle."""
    _state["activity"] = what


def current():
    return _state["activity"]
