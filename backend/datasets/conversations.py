"""Multi-turn conversation scenarios for the conversational metrics.

Each scenario lists the user's turns; the runner sends them to the chatbot one
at a time (with the conversation so far) and the judge scores the whole
exchange against the scenario and its expected outcome.

Like the golden answers: the shipped scenarios are in conversations.default.json,
edits go to conversations.json, and reset_to_defaults() (called on every side
panel load) restores the shipped themes while keeping other themes.
"""
from __future__ import annotations

import json
import os
import shutil
import threading
import uuid

_DIR = os.path.dirname(__file__)
CONVERSATIONS_PATH = os.path.join(_DIR, "conversations.json")
DEFAULT_CONVERSATIONS_PATH = os.path.join(_DIR, "conversations.default.json")
_LOCK = threading.RLock()


def _read_all() -> list[dict]:
    if not os.path.exists(CONVERSATIONS_PATH):
        shutil.copyfile(DEFAULT_CONVERSATIONS_PATH, CONVERSATIONS_PATH)
    with open(CONVERSATIONS_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _write_all(rows: list[dict]) -> None:
    tmp_path = CONVERSATIONS_PATH + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(rows, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, CONVERSATIONS_PATH)
    except BaseException:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


def load_conversations(theme: str | None = None) -> list[dict]:
    with _LOCK:
        rows = _read_all()
    if theme is None:
        return rows
    return [r for r in rows if r["theme"] == theme]


def _clean(fields: dict) -> dict:
    return {
        "theme": fields["theme"],
        "name": fields["name"].strip(),
        "scenario": fields.get("scenario", "").strip(),
        "chatbot_role": fields.get("chatbot_role", "").strip(),
        "expected_outcome": fields.get("expected_outcome", "").strip(),
        "user_turns": [t.strip() for t in fields["user_turns"] if t.strip()],
    }


# Scenarios built from goldens for a theme with none of its own.
AUTO_SCENARIOS = 3
DEFAULT_ROLE = "a chatbot assistant"
_ASK_AGAIN = "Going back to my first question: "


def from_goldens(goldens: list[dict], theme: str, chatbot_role: str = "",
                 limit: int = AUTO_SCENARIOS) -> list[dict]:
    """Multi-turn scenarios from a theme's goldens: two of its questions, then
    the first one again, so the multi-turn metrics can check the bot keeps its
    answers straight (and remembers what was asked) without a hand-written
    scenario. Not stored; add_conversation saves them when wanted."""
    usable = [g for g in goldens if (g.get("question") or "").strip()][: max(0, limit) * 2]
    built = []
    for first, second in zip(usable[::2], usable[1::2] + [None]):
        asked = [first] + ([second] if second else [])
        expected = " ".join(f"Question {n}: {g['expected_answer'].strip()}"
                            for n, g in enumerate(asked, 1) if (g.get("expected_answer") or "").strip())
        built.append({
            "id": "auto_" + "_".join(str(g.get("id", n)) for n, g in enumerate(asked)),
            "theme": theme,
            "name": f"From goldens: {first['question'].strip()[:60]}",
            "scenario": "A user asks questions this chatbot should handle, then comes back to the "
                        "first one to check the answer stays the same.",
            "chatbot_role": chatbot_role.strip() or DEFAULT_ROLE,
            "expected_outcome": f"The bot answers each question correctly ({expected}) and, when "
                                "the first question comes back, gives the same answer again "
                                "without asking the user to repeat anything.",
            "user_turns": [g["question"].strip() for g in asked]
                          + [_ASK_AGAIN + first["question"].strip()],
            "generated_from": "goldens",
        })
    return built


def add_conversation(fields: dict) -> dict:
    row = {"id": f"c_{uuid.uuid4().hex[:8]}", **_clean(fields)}
    with _LOCK:
        rows = _read_all()
        rows.append(row)
        _write_all(rows)
    return row


def update_conversation(conversation_id: str, fields: dict) -> dict | None:
    with _LOCK:
        rows = _read_all()
        for i, row in enumerate(rows):
            if row["id"] == conversation_id:
                rows[i] = {"id": conversation_id, **_clean(fields)}
                _write_all(rows)
                return rows[i]
    return None


def delete_conversation(conversation_id: str) -> bool:
    with _LOCK:
        rows = _read_all()
        kept = [r for r in rows if r["id"] != conversation_id]
        if len(kept) == len(rows):
            return False
        _write_all(kept)
    return True


def reset_to_defaults() -> int:
    """Puts every shipped theme back to its default scenarios; returns how many."""
    with open(DEFAULT_CONVERSATIONS_PATH, "r", encoding="utf-8") as f:
        defaults = json.load(f)
    shipped = {row["theme"] for row in defaults}
    with _LOCK:
        kept = [row for row in _read_all() if row["theme"] not in shipped]
        _write_all(defaults + kept)
    return len(defaults)
