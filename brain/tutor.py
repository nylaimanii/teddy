"""Homework tutor: hints and guiding questions, never the answer.

Math: Cortex (AI_COMPLETE) plans 2-4 tiny questions that lead to the answer; Python checks every number
exactly, so Teddy can't be wrong and never has to say the final answer himself.
Other subjects: Cortex Search over the homework notes -> one guiding question, then gentle feedback.
"""
import ast
import json
import operator
import re
from fractions import Fraction

from brain import snowflake as sf

_UNITS = {w: i for i, w in enumerate("zero one two three four five six seven eight nine ten eleven twelve thirteen "
                                     "fourteen fifteen sixteen seventeen eighteen nineteen".split())}
_TENS = {w: 10 * i for i, w in enumerate("_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()) if i > 1}
_FRAC_WORDS = {"half": Fraction(1, 2), "halves": Fraction(1, 2), "third": Fraction(1, 3), "thirds": Fraction(1, 3),
               "quarter": Fraction(1, 4), "quarters": Fraction(1, 4), "fourth": Fraction(1, 4), "fourths": Fraction(1, 4)}


def parse_number(text):
    """'fifty-six' / '56' / 'it's 3/4' / 'one hundred and two' / 'a half' -> Fraction, else None."""
    t = (text or "").lower().replace("-", " ").replace(",", "")
    m = re.search(r"(-?\d+)\s*/\s*(\d+)", t)
    if m and int(m.group(2)):
        return Fraction(int(m.group(1)), int(m.group(2)))
    m = re.search(r"-?\d+(\.\d+)?", t)
    if m:
        return Fraction(m.group(0))
    total, cur, seen = 0, 0, False
    for w in re.findall(r"[a-z]+", t):
        if w in _UNITS:
            cur += _UNITS[w]; seen = True
        elif w in _TENS:
            cur += _TENS[w]; seen = True
        elif w == "hundred" and seen:
            cur = max(cur, 1) * 100
        elif w == "thousand" and seen:
            total += max(cur, 1) * 1000; cur = 0
        elif w in _FRAC_WORDS and (seen or re.search(rf"\b(a|one)\s+{w}", t)):
            return Fraction(max(cur, 1)) * _FRAC_WORDS[w]
    return Fraction(total + cur) if seen else None


def fmt(v):
    v = Fraction(v)
    return str(v.numerator) if v.denominator == 1 else f"{v.numerator}/{v.denominator}"


_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv}


def _eval(expr):
    def ev(n):
        if isinstance(n, ast.Expression):
            return ev(n.body)
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)):
            return Fraction(str(n.value))
        if isinstance(n, ast.BinOp) and type(n.op) in _OPS:
            return _OPS[type(n.op)](ev(n.left), ev(n.right))
        if isinstance(n, ast.BinOp) and isinstance(n.op, ast.Pow) and abs(ev(n.right)) <= 10:
            return ev(n.left) ** int(ev(n.right))
        if isinstance(n, ast.UnaryOp) and isinstance(n.op, ast.USub):
            return -ev(n.operand)
        raise ValueError
    try:
        return ev(ast.parse(expr.replace("×", "*").replace("÷", "/"), mode="eval"))
    except Exception:
        return None


def pretty(expr):
    """'40+16' -> '40 + 16', '7*8' -> '7 × 8', '56/8' -> '56 ÷ 8', but '3/4+1/4' keeps its fractions."""
    division = re.fullmatch(r"\s*\d+\s*/\s*\d+\s*", expr)
    e = expr.replace("**2", "²").replace("**3", "³").replace("*", " × ").replace("+", " + ")
    e = re.sub(r"(?<=[\d)²³])\s*-\s*", " − ", e)
    if division:
        e = e.replace("/", " ÷ ")
    return re.sub(r"\s+", " ", e).strip()


def _unused_check_step(st, truth, last):
    """Keep a Cortex step only if its expected number is right; hide the number from the card."""
    exp = parse_number(str(st.get("expect")))
    calc = _eval(st.get("calc") or "") if st.get("calc") else None
    if last:
        exp = truth
    elif calc is not None:
        exp = calc  # trust the arithmetic, not the model's number
    if exp is None:
        return None
    show = str(st.get("show") or "").strip()
    show = re.sub(rf"=\s*{re.escape(fmt(exp))}\s*$", "= ?", show)
    if not show.endswith("?"):
        show = (show + " = ?") if "=" not in show else show
    if re.search(rf"(?<![\d/]){re.escape(fmt(exp))}(?![\d/])", show.replace("= ?", "")) and last:
        show = f"{pretty(st.get('calc') or '')} = ?" if st.get("calc") else "= ?"
    return {"show": show, "ask": sf._spoken(st.get("ask") or "What do you think?"),
            "hint": sf._spoken(st.get("hint") or "Try breaking it into smaller pieces!"), "expect": fmt(exp)}


def _st(calc, ask, hint):
    return {"show": f"{pretty(calc)} = ?", "ask": ask, "hint": hint, "expect": fmt(_eval(calc))}


def _scaffold(expr):
    """Classroom strategies for two whole numbers: instant and always right. None -> ask Cortex."""
    m = re.fullmatch(r"\s*(\d+)\s*([-+*/])\s*(\d+)\s*", expr)
    if not m:
        return None
    a, op, b = int(m.group(1)), m.group(2), int(m.group(3))
    if op == "+":
        if a < 10 and b < 10:
            big, small = max(a, b), min(a, b)
            return [_st(f"{a}+{b}", f"What's {a} plus {b}?", f"Start at {big} and count up {small} more on your fingers!")]
        ta, tb, oa, ob = a // 10 * 10, b // 10 * 10, a % 10, b % 10
        return [_st(f"{ta}+{tb}", f"Let's do the tens first. What's {ta} plus {tb}?", f"Count by tens: {ta}, {ta + 10}…"),
                _st(f"{oa}+{ob}", f"Now the ones. What's {oa} plus {ob}?", f"Start at {max(oa, ob)} and count up {min(oa, ob)}."),
                _st(f"{ta + tb}+{oa + ob}", f"Now put them together. What's {ta + tb} plus {oa + ob}?",
                    f"Start at {ta + tb} and add {oa + ob} more.")]
    if op == "-":
        if b < 10 or a < b:
            return [_st(f"{a}-{b}", f"What's {a} take away {b}?", f"Start at {a} and count back {b} on your fingers!")]
        tb, ob = b // 10 * 10, b % 10
        steps = [_st(f"{a}-{tb}", f"Let's take away the tens first. What's {a} minus {tb}?", f"Count back by tens from {a}.")]
        if ob:
            steps.append(_st(f"{a - tb}-{ob}", f"Now take away the ones. What's {a - tb} minus {ob}?",
                             f"Start at {a - tb} and count back {ob}."))
        return steps
    if op == "*":
        n, k = max(a, b), min(a, b)  # k groups of n
        if k <= 1 or n > 100:
            return [_st(f"{a}*{b}", f"What's {a} times {b}?", f"That's {k} group{'s' * (k != 1)} of {n}!")]
        if k <= 5:
            steps, total = [], n
            for i in range(2, k + 1):
                steps.append(_st(f"{total}+{n}", f"{'Let' + chr(39) + 's add ' + str(n) + 's! ' if i == 2 else ''}What's {total} plus {n}?",
                                 f"Start at {total} and count up {n}."))
                total += n
            return steps
        return [_st(f"5*{n}", f"{a} times {b} is {k} groups of {n}. First, what's 5 times {n}?",
                    f"Count by {n}s five times, or half of 10 times {n}!"),
                _st(f"{k - 5}*{n}", f"Now the other {k - 5} group{'s' * (k - 5 != 1)}. What's {k - 5} times {n}?",
                    f"Count by {n}s, {k - 5} time{'s' * (k - 5 != 1)}."),
                _st(f"{5 * n}+{(k - 5) * n}", f"Put them together! What's {5 * n} plus {(k - 5) * n}?",
                    f"Start at {5 * n} and add {(k - 5) * n}.")]
    if op == "/" and b and a % b == 0:
        return [_st(f"{a}/{b}", f"What times {b} makes {a}?", f"Count by {b}s until you reach {a}. How many jumps?")]
    return None


def math_plan(question, expr, truth):
    """-> [{"show","ask","hint","expect"}]: small questions that end with the kid finding the answer."""
    steps = _scaffold(expr)
    if steps and steps[-1]["expect"] == fmt(truth):
        return steps
    p = pretty(expr)
    prompt = (
        f"A kid (age 5-10) is doing homework: \"{question}\" which is {p}. You are Teddy, their chill big-buddy tutor.\n"
        "Plan 2 to 4 tiny steps that help the kid figure it out THEMSELVES. Each step asks the kid ONE question "
        "they answer with a number. Never say the final answer anywhere.\n"
        "Each step: \"show\" = a short math line for the screen ending in \"= ?\" (like \"8 + 8 = ?\"), "
        "\"ask\" = one short spoken question, \"hint\" = one short hint that does not give the number, "
        "\"calc\" = the plain arithmetic for that step's answer using only numbers and + - * / (like \"8+8\"), "
        "\"expect\" = that step's number. The last step's answer is the answer to the whole problem.\n"
        "Return ONLY JSON: {\"steps\": [{\"show\": \"\", \"ask\": \"\", \"hint\": \"\", \"calc\": \"\", \"expect\": \"\"}]}")
    try:
        raw = sf.complete(prompt, temperature=0)
        steps = json.loads(re.search(r"\{.*\}", raw, re.S).group(0))["steps"][:4]
        out = []
        for i, st in enumerate(steps):
            last = i == len(steps) - 1
            calc_v = _eval(st.get("calc") or "")
            if calc_v is None or (not last and calc_v == truth) or (last and calc_v != truth):
                continue
            out.append({"show": f"{pretty(st['calc'])} = ?", "ask": sf._spoken(st.get("ask") or "What do you think?"),
                        "hint": sf._spoken(st.get("hint") or "Try breaking it into smaller pieces!"),
                        "expect": fmt(calc_v)})
        if out and out[-1]["expect"] == fmt(truth):
            return out
    except Exception as e:
        print(f"[tutor] plan failed, using simple plan: {e}")
    return [{"show": f"{p} = ?", "ask": f"What do you think {p.replace('×', 'times').replace('÷', 'divided by')} is?",
             "hint": "Try breaking it into smaller pieces, or count it out on your fingers!", "expect": fmt(truth)}]


def concept_hint(question):
    """Non-math homework: one guiding question from the homework notes (Cortex Search), never the answer."""
    b = sf.backend()
    try:
        hits = b.search(question, "homework") or []
    except Exception:
        hits = []
    src = "\n\n".join(h["chunk"] for h in hits[:3])
    prompt = (f"A kid (age 5-10) asked for homework help: \"{question}\"\n"
              + (f"Helpful notes:\n{src}\n" if src else "")
              + "You are Teddy, a chill big-buddy tutor. Do NOT give the answer. Reply with ONE short guiding "
              "question or hint (max 2 short sentences, simple words) that helps them think it through.")
    try:
        return sf._spoken(sf.complete(prompt, temperature=0.3)), [h.get("title") for h in hits[:1]]
    except Exception:
        return "Ooh, good question! What do you already know about it?", []


def concept_feedback(question, answer, hint):
    """Kid answered a non-math question -> {"correct": bool, "reply": short encouraging line, no answer given}."""
    prompt = (f"Homework question: \"{question}\". Teddy's hint was: \"{hint}\". The kid (age 5-10) answered: "
              f"\"{answer}\".\nIs the kid basically right? Reply ONLY JSON {{\"correct\": true/false, \"reply\": "
              "\"one short encouraging sentence; if wrong give a new small hint, never the answer\"}}")
    try:
        r = json.loads(re.search(r"\{.*\}", sf.complete(prompt, temperature=0), re.S).group(0))
        return {"correct": bool(r.get("correct")), "reply": sf._spoken(r.get("reply", ""))}
    except Exception:
        return {"correct": False, "reply": "Ooh, interesting! Tell me more about why you think that."}
