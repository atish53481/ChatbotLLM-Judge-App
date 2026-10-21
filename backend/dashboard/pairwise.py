"""Pairwise A/B judging: the same goldens go to chatbot A and chatbot B, and the
judge picks the better reply.

LLM judges favour the reply shown first (position bias) and longer replies
(verbosity bias). Every case is judged twice with the order swapped; a reply
wins only when it wins both orderings, otherwise the case is a tie. A winner
much longer than the loser carries a verbosity note.
"""
from __future__ import annotations

import time

from backend import usage
from backend.dashboard.runner import RunCancelled, _judge_name
from backend.judges.ask import ask_json
from backend.targets.http_client import MessageTooLong

DEFAULT_CRITERIA = (
    "Which reply better answers the user's message: correct, relevant, clear and helpful, "
    "without inventing facts? Length alone is not a reason to prefer a reply."
)
# A winner this many times longer than the loser gets a verbosity note.
VERBOSITY_RATIO = 1.5

PAIR_PROMPT = """You compare two chatbot replies to the same user message.

Criteria: {criteria}

User message:
{question}
{reference}
Reply 1:
{first}

Reply 2:
{second}

Return only JSON: {{"winner": "1" | "2" | "tie", "reason": "<one sentence>"}}"""


def _verdict(judge, question: str, first: str, second: str, criteria: str, expected: str) -> tuple[str, str]:
    reference = f"\nReference answer (for correctness):\n{expected}\n" if expected else ""
    data = ask_json(judge, PAIR_PROMPT.format(criteria=criteria, question=question, reference=reference,
                                              first=first, second=second))
    winner = str(data.get("winner", "tie")).strip().lower()
    return (winner if winner in ("1", "2") else "tie"), str(data.get("reason", ""))


def judge_pair(judge, question: str, reply_a: str, reply_b: str,
               criteria: str = DEFAULT_CRITERIA, expected: str = "") -> dict:
    """Both orderings; "a" / "b" only when they agree, else "tie"."""
    first, reason_ab = _verdict(judge, question, reply_a, reply_b, criteria, expected)
    second, reason_ba = _verdict(judge, question, reply_b, reply_a, criteria, expected)
    a_wins = (first == "1", second == "2")
    b_wins = (first == "2", second == "1")
    if all(a_wins):
        winner = "a"
    elif all(b_wins):
        winner = "b"
    else:
        winner = "tie"
    note = None
    if winner == "tie" and "tie" not in (first, second):
        note = "The judge picked the reply shown first both times (position bias), so this counts as a tie."
    if winner != "tie":
        win, lose = (reply_a, reply_b) if winner == "a" else (reply_b, reply_a)
        if len(win) > VERBOSITY_RATIO * max(1, len(lose)):
            note = (f"The winner is {len(win) / max(1, len(lose)):.1f}x longer than the other reply: "
                    "check it is better, not just longer.")
    return {"winner": winner, "orderings": [first, second], "reasons": [reason_ab, reason_ba], "note": note}


def run_pairwise(judge, target_a, target_b, items: list[dict], prompt, criteria: str = "",
                 on_progress=None, names: tuple[str, str] = ("A", "B")) -> dict:
    """Sends every item to both chatbots and judges each pair. Raises
    RunCancelled from on_progress; any other failure becomes an error dict."""
    report = on_progress or (lambda *_args: None)
    criteria = criteria.strip() or DEFAULT_CRITERIA
    usage_before, started = usage.snapshot(), time.monotonic()
    rows, skipped = [], 0
    try:
        for done, item in enumerate(items):
            question = item["question"]
            sent = prompt(item)
            report(done, len(items), "chat", question)
            try:
                reply_a = target_a.chat(sent).reply
                reply_b = target_b.chat(sent).reply
            except MessageTooLong:
                skipped += 1
                continue
            report(done, len(items), "judge", question)
            verdict = judge_pair(judge, sent, reply_a, reply_b, criteria, item.get("expected_answer", ""))
            rows.append({"question": question, "golden_id": item.get("id"), "input": sent,
                         "reply_a": reply_a, "reply_b": reply_b, **verdict})
    except RunCancelled:
        raise
    except Exception as e:  # noqa: BLE001 - a chatbot or judge failure is a run error
        return {"key": "pairwise", "status": "error", "error": f"{type(e).__name__}: {e}",
                "score": None, "rows": rows}
    wins_a = sum(1 for r in rows if r["winner"] == "a")
    wins_b = sum(1 for r in rows if r["winner"] == "b")
    ties = len(rows) - wins_a - wins_b
    after = usage.snapshot()
    decided = wins_a + wins_b
    return {
        "key": "pairwise",
        "status": "done" if rows else "error",
        "error": None if rows else "nothing was compared (every case was too long for a chatbot)",
        "names": {"a": names[0], "b": names[1]},
        "criteria": criteria,
        "wins_a": wins_a, "wins_b": wins_b, "ties": ties,
        # Share of decided cases chatbot A won (ties left out); None when all tied.
        "score": round(wins_a / decided, 4) if decided else None,
        "winner": "a" if wins_a > wins_b else "b" if wins_b > wins_a else "tie",
        "cases_run": len(rows), "cases_skipped": skipped,
        "judge": {"model": _judge_name(judge),
                  "tokens": after["judge_tokens"] - usage_before["judge_tokens"],
                  "calls": after["judge_calls"] - usage_before["judge_calls"]},
        "duration_s": round(time.monotonic() - started, 2),
        "rows": rows,
    }
