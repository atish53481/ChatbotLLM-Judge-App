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


class FakeJudge:
    """Answers with whichever payload was given; records every call."""

    def __init__(self, dataset=None, verdicts=None, outcomes=None, raw=None):
        self.payload = (dataset if dataset is not None else
                        {"verdicts": verdicts} if verdicts is not None else
                        {"outcomes": outcomes} if outcomes is not None else None)
        self.raw, self.calls = raw, []

    def generate(self, prompt):
        self.calls.append(prompt)
        return (self.raw if self.raw is not None else json.dumps(self.payload)), 0.0


def test_write_dataset_drops_duplicates_and_long_facts():
    judge = FakeJudge(dataset={"goldens": [
        {"question": "Do you ship to Canada?", "expected_answer": "Yes, 10-14 days.", "category": "happy_path"},
        {"question": "do you ship to canada", "expected_answer": "Yes.", "category": "edge_case"},
        {"question": "Refund window?", "expected_answer": "30 days.", "category": "fact"},
        {"question": "Express cost?", "expected_answer": " ".join(["word"] * 16), "category": "fact"}],
        "scenarios": []})
    goldens, _ = autogen.write_dataset(judge, [], [("u", "text")], n_goldens=5, n_scenarios=3, n_short=3,
                                       existing=["Refund window?"])
    assert [g["question"] for g in goldens] == ["Do you ship to Canada?"]
    assert goldens[0]["categories"] == ["happy_path"]
    prompt = judge.calls[0]
    assert "never quote" in prompt.lower() and "out_of_scope" in prompt and "multi_intent" in prompt


def test_check_grounded():
    judge = FakeJudge(verdicts=["supported", "unsupported"])
    rows = [{"question": "a", "expected_answer": "b"}] * 2
    assert autogen.check_grounded(judge, rows, "material") == [True, False]
    no_call = FakeJudge()
    assert autogen.check_grounded(no_call, rows[:1], "") == [False] and no_call.calls == []
    assert autogen.check_grounded(FakeJudge(verdicts=["supported"]), rows, "m") == [False, False]
    assert autogen.check_grounded(FakeJudge(raw="nope"), rows, "m") == [False, False]


def test_repair_fills_only_empty_fields():
    sc = [{"id": "c1", "chatbot_role": "", "expected_outcome": "Typed outcome", "user_turns": ["a", "b"]},
          {"id": "c2", "chatbot_role": "Typed role", "expected_outcome": "", "user_turns": ["a", "b"]}]
    judge = FakeJudge(outcomes={"c2": "Gets the refund timeline."})
    assert dict(autogen.repair_scenarios(judge, sc, "Shop bot.", "m")) == {
        "c1": {"chatbot_role": "Shop bot."}, "c2": {"expected_outcome": "Gets the refund timeline."}}
    # malformed outcomes: role fix stays, no outcome field
    bad = autogen.repair_scenarios(FakeJudge(raw="nope"), sc, "Shop bot.", "m")
    assert dict(bad) == {"c1": {"chatbot_role": "Shop bot."}}


def test_write_dataset_malformed_json_raises():
    with pytest.raises(Exception):
        autogen.write_dataset(FakeJudge(raw="not json"), [], [("u", "t")], n_goldens=5, n_scenarios=3, n_short=3)


def test_write_dataset_caps_facts_apart_from_the_other_goldens():
    others = [{"question": f"Question number {i} about shoes?", "expected_answer": "Explains it.",
               "category": "happy_path"} for i in range(6)]
    facts = [{"question": f"Fact {w}?", "expected_answer": "Yes.", "category": "Fact"} for w in ("one", "two")]
    judge = FakeJudge(dataset={"goldens": others + facts, "scenarios": []})
    goldens, _ = autogen.write_dataset(judge, [], [("u", "t")], n_goldens=2, n_scenarios=0, n_short=2)
    cats = [g["categories"][0] for g in goldens]
    assert cats.count("Fact") == 2 and cats.count("happy_path") == 4  # facts kept, not cut first
    long_fact = FakeJudge(dataset={"goldens": [{"question": "Cost?", "expected_answer": " ".join(["w"] * 16),
                                                "category": "FACT"}], "scenarios": []})
    assert autogen.write_dataset(long_fact, [], [("u", "t")], n_goldens=1, n_scenarios=0, n_short=1)[0] == []


def test_repair_writes_context_for_the_scenarios_that_need_it():
    sc = [{"id": "c1", "chatbot_role": "r", "expected_outcome": "o", "user_turns": ["a", "b"]},
          {"id": "c2", "chatbot_role": "r", "expected_outcome": "", "user_turns": ["a", "b"]}]
    material = "Alpha one.\nBeta   two. Gamma three. Delta four."
    judge = FakeJudge(dataset={"outcomes": {"c2": "Gets it."},
                               "contexts": {"c1": ["alpha one.", "Beta two.", "Invented fact.", "Gamma three.",
                                                   "Delta four."], "c2": ["Alpha one."]}})
    fixes = dict(autogen.repair_scenarios(judge, sc, "Shop bot.", material, need_context=["c1"]))
    # Only passages found in the material (case and spacing aside), at most 3; c2 did not ask for any.
    assert fixes == {"c1": {"context": ["alpha one.", "Beta two.", "Gamma three."]},
                     "c2": {"expected_outcome": "Gets it."}}
    assert len(judge.calls) == 1 and "passages" in judge.calls[0]
    # Context only: still one call; an invented passage is dropped; a long one is cut to 1500 chars.
    long = "word " * 400
    only = FakeJudge(dataset={"contexts": {"c1": ["Not in the material.", long]}})
    got = dict(autogen.repair_scenarios(only, sc[:1], "", "Intro. " + long, need_context=["c1"]))
    assert got == {"c1": {"context": [long.strip()[:autogen.PASSAGE_CHARS]]}} and len(only.calls) == 1
    invented = FakeJudge(dataset={"contexts": {"c1": ["Made up."]}})
    assert autogen.repair_scenarios(invented, sc[:1], "", "m", need_context=["c1"]) == []
    # Nothing to repair: no call.
    none = FakeJudge(dataset={})
    assert autogen.repair_scenarios(none, sc[:1], "Shop bot.", "m") == [] and none.calls == []


def test_interview_lets_stop_through():
    from backend.cancel import RunCancelled

    class Stopped:
        def chat(self, message, history=None):
            raise RunCancelled("stop")
    with pytest.raises(RunCancelled):
        autogen.interview(Stopped())
