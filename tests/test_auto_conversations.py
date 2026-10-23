"""Conversation scenarios built from a theme's goldens, when it has none of its own."""
from backend import metrics_catalog
from backend.datasets import conversations
from backend.datasets import goldens as goldens_store
from tests.test_app import app_module, client  # noqa: F401 - fixtures


def _golden(n, context=()):
    return {"id": f"g{n}", "theme": "bot", "question": f"Question {n}?",
            "expected_answer": f"Answer {n}.", "context": list(context)}


def test_from_goldens_asks_the_first_question_again():
    built = conversations.from_goldens([_golden(1), _golden(2), _golden(3)], "bot",
                                       chatbot_role="shop support bot")
    first, second = built
    assert first["user_turns"] == ["Question 1?", "Question 2?",
                                   "Going back to my first question: Question 1?"]
    assert "Answer 1." in first["expected_outcome"] and "Answer 2." in first["expected_outcome"]
    assert first["chatbot_role"] == "shop support bot" and first["theme"] == "bot"
    assert first["generated_from"] == "goldens"
    # An odd golden out still makes a scenario: its question, then asked again.
    assert second["user_turns"] == ["Question 3?", "Going back to my first question: Question 3?"]


def test_from_goldens_caps_the_count_and_needs_goldens():
    assert len(conversations.from_goldens([_golden(n) for n in range(10)], "bot")) == conversations.AUTO_SCENARIOS
    assert conversations.from_goldens([], "bot") == []
    assert conversations.from_goldens([_golden(1)], "bot")[0]["chatbot_role"] == conversations.DEFAULT_ROLE


def test_cases_fall_back_to_goldens_only_without_own_scenarios():
    goldens_store.add_golden("auto_theme", "Do you ship abroad?", "Yes, in 10-14 days.")
    spec = metrics_catalog.get_spec("knowledge_retention")
    built = spec.cases(theme="auto_theme", persona="travel bot")
    assert built and built[0]["generated_from"] == "goldens"
    assert built[0]["chatbot_role"] == "travel bot"
    assert metrics_catalog.get_spec("role_adherence").cases(theme="auto_theme")  # has a role
    conversations.add_conversation({"theme": "auto_theme", "name": "Mine", "user_turns": ["Hi"]})
    own = spec.cases(theme="auto_theme")
    assert [c["name"] for c in own] == ["Mine"]


def test_generate_endpoint_saves_scenarios(client):
    goldens_store.add_golden("gen_theme", "What are your hours?", "9 to 5.")
    goldens_store.add_golden("gen_theme", "Where are you based?", "In Pune.")
    r = client.post("/api/conversations/from-goldens", json={"theme": "gen_theme", "chatbot_role": "help desk"})
    assert r.status_code == 200, r.text
    saved = r.json()
    assert len(saved) == 1 and saved[0]["id"].startswith("c_")
    assert saved[0]["user_turns"][-1] == "Going back to my first question: What are your hours?"
    assert client.get("/api/conversations", params={"theme": "gen_theme"}).json() == saved
    empty = client.post("/api/conversations/from-goldens", json={"theme": "no_goldens_here"})
    assert empty.status_code == 400
