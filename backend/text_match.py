"""Reference text-match scorers: how close the reply's wording is to the
golden's expected answer. No judge and no tokens.

Exact Match, ROUGE-L and BLEU are DeepEval's own (`deepeval.scorer.Scorer`;
ROUGE-L needs the rouge-score package, BLEU nltk plus its punkt_tab data,
downloaded on first use).

They measure overlap of words, not meaning, so a correct answer
in other words scores low. Use them for replies that should be near-verbatim
(fixed policy text, codes, short facts); the judge-based metrics are the better
test of a free-form answer.
"""
from __future__ import annotations

import os
import re
import sys
import warnings

from deepeval.metrics import BaseMetric
from deepeval.scorer import Scorer
from deepeval.test_case import LLMTestCase, SingleTurnParams

def _words(text: str) -> list[str]:
    return re.findall(r"\w+", (text or "").casefold())


def exact_match(actual: str, expected: str) -> float:
    """1.0 when the texts match ignoring case, spacing, punctuation and a/an/the
    (DeepEval's quasi exact match)."""
    if not actual.strip() and not expected.strip():
        return 1.0
    return float(Scorer.quasi_exact_match_score(expected, actual))


def rouge_l(actual: str, expected: str) -> float:
    """ROUGE-L F1 over stemmed words (DeepEval's rouge_score, "rougeL")."""
    if not _words(actual) and not _words(expected):
        return 1.0
    return float(Scorer.rouge_score(expected, actual, "rougeL"))


_PUNKT_READY = False


def _punkt_ready() -> None:
    """NLTK's word tokenizer (used by DeepEval's BLEU) needs the punkt_tab data;
    fetched once into the environment's own nltk_data folder."""
    global _PUNKT_READY
    if _PUNKT_READY:
        return
    import nltk
    try:
        nltk.data.find("tokenizers/punkt_tab")
    except LookupError:
        if not nltk.download("punkt_tab", download_dir=os.path.join(sys.prefix, "nltk_data"), quiet=True):
            raise RuntimeError("BLEU needs NLTK's punkt_tab tokenizer data and it could not be downloaded. "
                               "Run: .venv\\Scripts\\python -m nltk.downloader -d .venv\\nltk_data punkt_tab")
    _PUNKT_READY = True


def bleu(actual: str, expected: str) -> float:
    """Sentence BLEU-1 (DeepEval's sentence_bleu_score default): unigram
    precision times the brevity penalty."""
    if not _words(actual) or not _words(expected):
        return 1.0 if _words(actual) == _words(expected) else 0.0
    _punkt_ready()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # nltk warns on zero overlap; the score is 0 then
        return float(Scorer.sentence_bleu_score(expected, actual, "bleu1"))


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
