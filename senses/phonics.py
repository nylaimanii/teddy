"""Sound words out for a kid learning to read: syllables("caterpillar") -> "cat-er-pil-lar" style splits.

Classroom rules, not a dictionary: vowel teams stay together, VC-CV splits between consonants,
V-CV splits before a lone consonant, blends/digraphs stay together, silent final e, consonant+le.
"""
import re

VOWELS = set("aeiou")
DIGRAPHS = {"ch", "sh", "th", "ph", "wh", "ck", "gh", "qu"}
BLENDS = {"bl", "br", "cl", "cr", "dr", "fl", "fr", "gl", "gr", "pl", "pr", "sc", "sk", "sl", "sm", "sn",
          "sp", "st", "sw", "tr", "tw", "str", "spr", "scr", "thr", "shr", "chr"}
SIGHT = {"because", "people", "little", "about", "again", "before", "every", "never", "other", "very",
         "after", "always", "around", "could", "would", "should", "their", "there", "where", "which"}


def _vowel_groups(w):
    """[(start, end)] of vowel runs; y counts as a vowel except at the start."""
    is_v = [c in VOWELS or (c == "y" and i > 0) for i, c in enumerate(w)]
    # silent final e (make, cause, adventure) -- but not "le" endings (table) or "ee"/"ie"
    if len(w) > 3 and w.endswith("e") and not is_v[-2] and not w.endswith("le"):
        is_v[-1] = False
    groups, i = [], 0
    while i < len(w):
        if is_v[i]:
            j = i
            while j + 1 < len(w) and is_v[j + 1]:
                j += 1
            groups.append((i, j + 1))
            i = j + 1
        else:
            i += 1
    return groups


def _split_point(cluster):
    """How many consonants of the cluster stay with the syllable on the left."""
    n = len(cluster)
    if n == 0:
        return 0
    if cluster == "x":
        return 1  # mox-i
    if n == 1 or cluster in DIGRAPHS:
        return n if cluster == "ck" else 0  # V-CV (o-pen), but pock-et
    for tail in (3, 2):  # keep a blend or digraph together on the right: um-brel-la, hun-gry
        if n > tail and (cluster[-tail:] in BLENDS or cluster[-tail:] in DIGRAPHS):
            return n - tail
    if n == 2:
        return 1  # VC-CV: yes-ter, won-der
    return 1


def syllables(word):
    w = word.lower()
    if not w.isalpha():
        return word
    groups = _vowel_groups(w)
    if len(groups) < 2:
        return w
    cuts = []
    for (_, a_end), (b_start, _) in zip(groups, groups[1:]):
        cuts.append(a_end + _split_point(w[a_end:b_start]))
    # consonant + le ending is its own syllable: ta-ble, bub-ble
    if re.search(r"[^aeiou]le$", w) and len(w) > 3:
        cuts = [c for c in cuts if c < len(w) - 3] + [len(w) - 3]
    parts, prev = [], 0
    for c in sorted(set(cuts)):
        if 0 < c < len(w):
            parts.append(w[prev:c])
            prev = c
    parts.append(w[prev:])
    return "-".join(p for p in parts if p)


def tricky_words(text, limit=3):
    """Longer multi-syllable words worth sounding out, in reading order, no repeats."""
    seen, out = set(), []
    for word in re.findall(r"[A-Za-z]+", text):
        lw = word.lower()
        if lw in seen or lw in SIGHT or len(lw) < 6 or "-" not in syllables(lw):
            continue
        seen.add(lw)
        out.append(lw)
    longest = set(sorted(out, key=len, reverse=True)[:limit])
    return [w for w in out if w in longest]


def reading_script(text):
    """What Teddy says: the words as written, then the tricky ones sounded out."""
    text = re.sub(r"\s+", " ", text.strip())  # a page's line breaks aren't sentence ends
    words = re.findall(r"[A-Za-z']+", text)
    if not words:
        return ""
    if len(words) == 1:
        w = words[0]
        s = syllables(w)
        return f"This word is {w}. " + (f"Let's sound it out: {s}. {w.capitalize()}!" if "-" in s else f"{w.capitalize()}!")
    if text[-1] not in ".!?":
        text += "."
    hard = tricky_words(text)
    if not hard:
        return text
    return text + " Let's sound out the tricky words. " + " ".join(f"{syllables(w)}. {w.capitalize()}!" for w in hard)


if __name__ == "__main__":
    for w in ["elephant", "hungry", "caterpillar", "beautiful", "dinosaur", "together", "yesterday", "umbrella",
              "adventure", "wonderful", "amoxicillin", "table", "bubble", "pocket", "rabbit", "butterfly", "happily"]:
        print(w, "->", syllables(w))
    print(reading_script("The elephant was\nvery hungry"))
    print(reading_script("butterfly"))
