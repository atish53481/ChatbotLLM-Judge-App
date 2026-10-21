"""Free-form judge questions that DeepEval metrics do not cover (pairwise
preference, attack turns, user simulation, paraphrases): one prompt in, one
JSON object out, through the same GroqJudge (rate-limit retries, usage count).
"""
from __future__ import annotations

from deepeval.metrics.utils import trimAndLoadJson


def ask_text(judge, prompt: str) -> str:
    reply = judge.generate(prompt)
    # DeepEval models return (text, cost); test doubles may return the text alone.
    text = reply[0] if isinstance(reply, tuple) else reply
    return str(text)


def ask_json(judge, prompt: str) -> dict:
    """The judge's reply parsed as a JSON object; ValueError when it is not one."""
    text = ask_text(judge, prompt)
    try:
        data = trimAndLoadJson(text)
    except Exception as e:  # noqa: BLE001 - DeepEval raises its own error types
        raise ValueError(f"the judge did not reply with JSON: {text[:200]!r}") from e
    if not isinstance(data, dict):
        raise ValueError(f"the judge did not reply with a JSON object: {text[:200]!r}")
    return data
