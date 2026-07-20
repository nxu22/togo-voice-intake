"""Detect when a caller has trailed off mid-sentence.

The end-of-turn model cannot help here. It scored "The company's name is" — an
obviously dangling phrase — at 0.96, i.e. confidently finished. No threshold catches a
confidently-wrong prediction, and the detector exposes no grammar or completeness
signal to lean on (only end_of_turn_probability and backchannel_probability).

So we look at the words instead. English sentences do not end on "is", "and", "the",
or "um". If the caller's last word cannot end a sentence, they are still thinking —
hold the line instead of interjecting.
"""

from __future__ import annotations

import re

# Words that essentially never end a spoken English sentence.
#
# The list is deliberately narrow. Auxiliaries and pronouns are EXCLUDED even though
# they often dangle, because they also end perfectly good short answers — "That's it",
# "Yes we do", "I have". Flagging those would make us sit silent on a complete reply,
# which is the failure mode we are trying to remove, not add.
_DANGLING_WORDS = frozenset(
    """
    a an the my our your their its
    is are was were am be been being
    to of for with at in on by from into onto through during between against within
    and or but so because if when while unless until though although than as
    like plus versus
    um uh er erm hmm uhh ah eh
    """.split()
)

_FILLERS = frozenset("um uh er erm hmm uhh ah eh".split())

# "so" is in the dangling set because it dangles as a conjunction ("we miss calls, so
# ..."). But it also ends these very common short answers — "I think so", "I guess so",
# "I hope so" — which are complete. Told apart by the verb right before it. Without this,
# "I think so." (a normal reply to "Sound good?") was held as unfinished and drew a nudge.
_SO_COMPLETERS = frozenset(
    "think thought guess hope believe suppose reckon said say".split()
)

# Deepgram may or may not punctuate. "The company's name is." is just as unfinished as
# "The company's name is", so judge the word, not the punctuation.
_TRAILING_PUNCT = re.compile(r"[\s.;:!?\-–—]+$")

# Digits are words too. A letters-only pattern silently dropped them, so
# "Best number is 204-555-0101" tokenized to [..., "is"] and the caller's phone number
# — the one required field — was read as a dangling sentence and held in silence.
_WORD = re.compile(r"[a-z0-9']+")


def looks_unfinished(text: str) -> bool:
    """True if the caller appears to have trailed off mid-sentence.

    Asymmetric by design: a false positive costs a few seconds of patient silence (and
    a nudge recovers it), while a false negative is what produced "I'm listening — go
    ahead" on top of a caller who was only thinking of their company name.
    """
    text = (text or "").strip()
    if not text:
        return False

    # Drawing breath mid-list: "Square, Excel,"
    if text.endswith(","):
        return True

    words = _WORD.findall(_TRAILING_PUNCT.sub("", text).lower())
    if not words:
        return False

    # A lone filler is never an answer.
    if len(words) == 1 and words[0] in _FILLERS:
        return True

    # "I think so" / "I guess so" — complete, despite ending on a normally-dangling "so".
    if words[-1] == "so" and len(words) >= 2 and words[-2] in _SO_COMPLETERS:
        return False

    return words[-1] in _DANGLING_WORDS
