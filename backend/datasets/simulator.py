"""Conversation simulator: the judge plays the user against the real chatbot.

A hand-written scenario fixes every user turn in advance, so it cannot follow
up on what the chatbot actually said. Here the judge reads the scenario, the
user's persona and the expected outcome, writes one user message at a time
from the chatbot's last reply, and stops when the outcome is reached (or the
turn cap is hit). The finished conversation can be scored with the
conversational metrics and saved as a fixed scenario for regression runs.
"""
from __future__ import annotations

from backend.judges.ask import ask_json

MAX_TURNS = 10
DEFAULT_TURNS = 5

USER_PROMPT = """You are role-playing a user talking to a chatbot, to test it.

Scenario: {scenario}
Who you are: {persona}
What you want by the end: {expected_outcome}

Conversation so far:
{transcript}

Write your next message as this user: natural, brief, one message, reacting to
what the chatbot just said. Set "done" to true only when what you wanted has
happened or the chatbot clearly cannot help.

Return only JSON: {{"message": "<your next message>", "done": false}}"""


def _transcript(turns: list[dict]) -> str:
    return "\n".join(f"{'User' if t['role'] == 'user' else 'Bot'}: {t['content']}" for t in turns)


def simulate(judge, target, scenario: dict, max_turns: int = DEFAULT_TURNS, on_turn=None) -> dict:
    """Runs one simulated conversation. scenario: {scenario, expected_outcome,
    user_persona?}. Returns {turns, user_turns, stop_reason}."""
    max_turns = max(1, min(MAX_TURNS, int(max_turns)))
    turns: list[dict] = []
    stop_reason = "turn limit"
    for turn in range(1, max_turns + 1):
        data = ask_json(judge, USER_PROMPT.format(
            scenario=scenario.get("scenario") or "A user contacts the chatbot for help.",
            persona=scenario.get("user_persona") or "An ordinary customer.",
            expected_outcome=scenario.get("expected_outcome") or "Get a useful answer.",
            transcript=_transcript(turns) or "(the conversation has not started)"))
        if turns and data.get("done") is True:
            stop_reason = "outcome reached"
            break
        message = str(data.get("message", "")).strip()
        if not message:
            stop_reason = "the simulated user had nothing more to say"
            break
        if on_turn:
            on_turn(turn, max_turns)
        reply = target.chat(message, history=list(turns)).reply
        turns += [{"role": "user", "content": message}, {"role": "assistant", "content": reply}]
    return {
        "turns": turns,
        "user_turns": [t["content"] for t in turns if t["role"] == "user"],
        "stop_reason": stop_reason,
    }


def score_conversation(spec, judge, scenario: dict, turns: list[dict], threshold: float | None = None,
                       metric_kwargs: dict | None = None) -> dict:
    """Scores a finished conversation with one conversational metric. Nothing
    is persisted: a simulated conversation differs every time, so it does not
    belong on the trend chart until it is saved as a fixed scenario."""
    from backend.metrics_catalog import deepeval_threshold, reported_score

    pass_mark = spec.threshold if threshold is None else threshold
    try:
        metric = spec.build_metric(judge, deepeval_threshold(spec, pass_mark), **(metric_kwargs or {}))
        metric.measure(spec.build_case(scenario, turns))
    except Exception as e:  # noqa: BLE001 - one failed metric must not lose the conversation
        return {"key": spec.key, "status": "error", "error": f"{type(e).__name__}: {e}", "score": None}
    return {
        "key": spec.key, "title": spec.title,
        "status": "pass" if metric.is_successful() else "fail",
        "score": reported_score(spec, metric.score), "threshold": pass_mark,
        "reason": metric.reason or "", "error": None,
    }
