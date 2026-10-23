"""Golden answers and conversation scenarios made for whichever chatbot is
connected: the app interviews the bot, looks for its FAQ / help / docs pages on
its own site, and has the judge write a dataset that fits it. Offline."""
import json
from unittest.mock import patch

import pytest

from backend.datasets import autogen
from backend.datasets import conversations as conversations_store
from backend.datasets import goldens as goldens_store
from backend.targets.base import ChatbotClient, ChatReply
from tests.test_app import (  # noqa: F401 - fixtures
    _create_target, app_module, client, documents_in_the_foreground)


class Bot(ChatbotClient):
    def __init__(self, fail=False):
        self.fail, self.asked = fail, []

    def chat(self, message, history=None):
        self.asked.append(message)
        if self.fail:
            raise ValueError("chatbot down")
        return ChatReply(reply="I help with Acme orders, returns and warranty.", model="m", mode="http")


class Judge:
    def __init__(self, payload):
        self.payload, self.prompts = payload, []

    def generate(self, prompt):
        self.prompts.append(prompt)
        return json.dumps(self.payload), 0.0


DATASET = {
    "goldens": [
        {"question": "How do I return an item?", "expected_answer": "Explains the return steps.", "category": "returns"},
        {"question": "  ", "expected_answer": "dropped: no question"},
    ],
    "scenarios": [
        {"name": "Return a broken grinder", "scenario": "Customer returns a broken item.",
         "expected_outcome": "Bot explains returns and warranty.", "chatbot_role": "Acme support",
         "user_turns": ["My grinder broke.", "Can I return it?"]},
        {"name": "dropped: one turn", "user_turns": ["hi"]},
    ],
}


def test_candidate_sites_come_from_the_chatbot_and_its_web_page():
    sites = autogen.candidate_sites({"url": "https://api.acme.com/v1/chat",
                                     "headers": {"Origin": "https://www.acme.com"}})
    assert sites[:2] == ["https://www.acme.com", "https://acme.com"]
    assert autogen.candidate_sites({"url": "http://127.0.0.1:8000/chat"}) == ["http://127.0.0.1:8000"]
    assert autogen.candidate_sites({"url": "https://x.com/c", "help_url": "https://help.x.com/a"})[0] == "https://help.x.com"


def test_find_help_pages_keeps_the_pages_that_answer():
    def fetch(url):
        if url.endswith("/faq"):
            return "Frequently asked questions " * 20, url
        raise ValueError("404")

    found = autogen.find_help_pages({"url": "https://acme.com/chat"}, fetch=fetch)
    assert found == [("https://acme.com/faq", "Frequently asked questions " * 20)]


def test_interview_asks_the_chatbot_and_survives_errors():
    bot = Bot()
    answers = autogen.interview(bot)
    assert len(answers) == len(autogen.INTERVIEW) and answers[0][1].startswith("I help with Acme")
    assert autogen.interview(Bot(fail=True)) == []


def test_write_dataset_cleans_what_the_judge_returns():
    judge = Judge(DATASET)
    goldens, scenarios = autogen.write_dataset(judge, [("What can you do?", "Acme orders.")],
                                               [("https://acme.com/faq", "Returns within 30 days.")], "Acme support")
    assert [g["question"] for g in goldens] == ["How do I return an item?"]
    assert [s["name"] for s in scenarios] == ["Return a broken grinder"]
    assert "Acme orders." in judge.prompts[0] and "Returns within 30 days." in judge.prompts[0]


@pytest.fixture
def offline(app_module, monkeypatch):
    judge = Judge(DATASET)
    monkeypatch.setattr(app_module, "build_judge", lambda **_kw: judge)
    monkeypatch.setattr(app_module, "_client_or_400", lambda row: Bot())
    return judge


def test_prepare_offers_to_learn_the_chatbot(client, offline):
    bot = _create_target(client, name="Acme Bot", config={"theme": "generic"})
    out = client.post(f"/api/targets/{bot['id']}/prepare-goldens", json={}).json()
    assert autogen.SOURCE_CHATBOT in out["can_generate"]
    assert autogen.SOURCE_SEARCH in out["can_generate"]  # no help page given: offer to look for one


def test_generate_builds_goldens_and_scenarios_for_this_chatbot(app_module, client, offline, tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "DOCUMENTS_DIR", tmp_path)
    bot = _create_target(client, name="Acme Bot", config={"theme": "generic"})
    page = ("https://acme.com/faq", "Returns are accepted within 30 days. " * 10)
    with patch.object(app_module, "find_help_pages", return_value=[page]), \
         patch.object(app_module, "generate_goldens_from_document", return_value=2):
        out = client.post(f"/api/targets/{bot['id']}/prepare-goldens", json={"generate": True}).json()
    theme = out["theme"]
    auto = [g for g in goldens_store.load_goldens(theme=theme) if g.get("source") == autogen.GOLDEN_SOURCE]
    assert [g["question"] for g in auto] == ["How do I return an item?"]
    assert [c["name"] for c in conversations_store.load_conversations(theme=theme)] == ["Return a broken grinder"]
    assert [d["filename"] for d in out["documents"]] == ["https://acme.com/faq"]
    saved = {t["id"]: t for t in client.get("/api/targets").json()}[bot["id"]]
    assert saved["config"]["help_url"] == "https://acme.com/faq"
    # Done once: asking again does not offer (or spend tokens on) the same work.
    again = client.post(f"/api/targets/{bot['id']}/prepare-goldens", json={}).json()
    assert autogen.SOURCE_CHATBOT not in again["can_generate"]
