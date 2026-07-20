"""The dangling-sentence heuristic.

The turn detector scored "The company's name is" at 0.96 — confidently finished. It
exposes no completeness signal, so the words are all we have.

The dangerous direction is the FALSE POSITIVE: holding the line on a complete answer
makes the agent sit mute on a caller who is waiting for it. Most of these tests guard
that direction.
"""

from __future__ import annotations

import pytest

from agent.turns import looks_unfinished


@pytest.mark.parametrize(
    "text",
    [
        # The one from the call.
        "The company's name is",
        "The company's name is.",  # Deepgram may punctuate it anyway
        # Trailing function words.
        "We're on Square and",
        "It's kind of like",
        "I handle it with",
        "The biggest problem is the",
        "We do it because",
        "My business is a",
        "That happens when",
        # "so" as a genuine conjunction still dangles (not preceded by think/guess/hope).
        "We keep missing calls so",
        "It takes forever so",
        # Mid-list breath.
        "Square, Excel,",
        # Pure hesitation.
        "um",
        "uh",
    ],
)
def test_trailed_off_mid_sentence(text):
    assert looks_unfinished(text) is True


@pytest.mark.parametrize(
    "text",
    [
        # Real answers to the seven questions.
        "I run a bakery.",
        "Sunrise Bakery, over on Corydon.",
        "Taking phone orders.",
        "We scribble them on a notepad by the register.",
        "Every single day.",
        "Square for payments, Excel for tracking.",
        "Dana Reyes, 204-555-0101.",
        # Contact answers END IN DIGITS. A letters-only tokenizer dropped them and read
        # the last word as "is", holding the caller's phone number in silence.
        "Dana Reyes. Best number is 204-555-0101.",
        "My number is 204 555 0101",
        "The best email is dana@sunrisebakery.ca",
        "You can reach me at 2045550101.",
        # Short answers that END on words a naive list would flag.
        "That's it.",  # ends "it"
        "Yes, we do.",  # ends "do"
        "I have.",  # ends "have"
        "We can.",  # ends "have"/aux family
        "Every day, all day.",
        "No.",
        "Yes.",
        "Sounds good.",
        # "so" dangles as a conjunction, but these are complete replies to "Sound good?".
        "I think so.",
        "I guess so.",
        "I hope so.",
        "Yeah, I believe so.",
        # A question back at us is a complete turn.
        "How much does this cost?",
        # Empty / silence is not "unfinished" — there is nothing to wait for.
        "",
        "   ",
    ],
)
def test_complete_answers_are_not_held(text):
    assert looks_unfinished(text) is False


def test_the_exact_failing_utterance_is_caught():
    """0.96 from the end-of-turn model. This is the whole reason the module exists."""
    assert looks_unfinished("The company's name is")


def test_a_filler_inside_a_real_answer_does_not_trigger():
    # "uh" only counts as hesitation when it's the entire utterance.
    assert looks_unfinished("uh, we use Excel") is False


def test_case_and_whitespace_do_not_matter():
    assert looks_unfinished("  THE COMPANY'S NAME IS  ") is True
    assert looks_unfinished("  I run a bakery.  ") is False
