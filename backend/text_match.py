"""Reference text-match scorers: how close the reply's wording is to the
golden's expected answer. Pure Python, no judge and no tokens.

They measure overlap of words or characters, not meaning, so a correct answer
in other words scores low. Use them for replies that should be near-verbatim
(fixed policy text, codes, short facts); the judge-based metrics are the better
test of a free-form answer.
"""
from __future__ import annotations

import math
import re
from collections import Counter

from deepeval.metrics import BaseMetric
from deepeval.test_case import LLMTestCase, SingleTurnParams

# Longest text compared character by character (Levenshtein is O(n·m)).
MAX_EDIT_CHARS = 2000


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip().casefold().rstrip(".!?")


def _words(text: str) -> list[str]:
    return re.findall(r"\w+", (text or "").casefold())


def exact_match(actual: str, expected: str) -> float:
    """1.0 when the texts match ignoring case, spacing and end punctuation."""
    return 1.0 if _normalize(actual) == _normalize(expected) else 0.0


def levenshtein_similarity(actual: str, expected: str) -> float:
    """1 − (character edits needed) / (length of the longer text)."""
    a, b = _normalize(actual)[:MAX_EDIT_CHARS], _normalize(expected)[:MAX_EDIT_CHARS]
    if not a and not b:
        return 1.0
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        for j, cb in enumerate(b, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (ca != cb)))
        previous = current
    return 1 - previous[-1] / max(len(a), len(b))


def _lcs(a: list[str], b: list[str]) -> int:
    previous = [0] * (len(b) + 1)
    for x in a:
        current = [0]
        for j, y in enumerate(b, 1):
            current.append(previous[j - 1] + 1 if x == y else max(previous[j], current[j - 1]))
        previous = current
    return previous[-1]


def rouge_l(actual: str, expected: str) -> float:
    """ROUGE-L F1: the longest common word sequence over both texts' lengths."""
    a, b = _words(actual), _words(expected)
    if not a or not b:
        return 1.0 if a == b else 0.0
    common = _lcs(a, b)
    if not common:
        return 0.0
    precision, recall = common / len(a), common / len(b)
    return 2 * precision * recall / (precision + recall)


def bleu(actual: str, expected: str, max_n: int = 4) -> float:
    """Sentence BLEU: geometric mean of 1..4-gram precision with add-one
    smoothing above unigrams, times the brevity penalty."""
    a, b = _words(actual), _words(expected)
    if not a or not b:
        return 1.0 if a == b else 0.0
    log_sum = 0.0
    for n in range(1, max_n + 1):
        grams = Counter(tuple(a[i:i + n]) for i in range(len(a) - n + 1))
        ref = Counter(tuple(b[i:i + n]) for i in range(len(b) - n + 1))
        overlap = sum(min(count, ref[g]) for g, count in grams.items())
        total = sum(grams.values())
        if n == 1:
            if not overlap:
                return 0.0
            precision = overlap / total
        else:
            precision = (overlap + 1) / (total + 1)
        log_sum += math.log(precision)
    brevity = 1.0 if len(a) > len(b) else math.exp(1 - len(b) / len(a))
    return min(1.0, brevity * math.exp(log_sum / max_n))


class TextMatchMetric(BaseMetric):
    """A DeepEval metric around one of the scorers above; never calls a model."""

    _required_params = [SingleTurnParams.ACTUAL_OUTPUT, SingleTurnParams.EXPECTED_OUTPUT]

    def __init__(self, name: str, scorer, threshold: float):
        self.name, self.scorer, self.threshold = name, scorer, threshold
        self.include_reason, self.async_mode, self.strict_mode = True, False, False
        self.evaluation_model = None
        self.score, self.reason, self.success, self.error = None, None, None, None

    def measure(self, test_case: LLMTestCase, *args, **kwargs) -> float:
        self.score = round(self.scorer(test_case.actual_output or "", test_case.expected_output or ""), 6)
        self.success = self.score >= self.threshold
        self.reason = (f"{self.name} {self.score:.2f} between the reply and the expected answer "
                       f"(word/character overlap, not meaning).")
        return self.score

    async def a_measure(self, test_case: LLMTestCase, *args, **kwargs) -> float:
        return self.measure(test_case)

    def is_successful(self) -> bool:
        return bool(self.success)

    @property
    def __name__(self):
        return self.name
