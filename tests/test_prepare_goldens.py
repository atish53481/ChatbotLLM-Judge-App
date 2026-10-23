"""Preparing a new chatbot's golden dataset: its own theme, starter goldens, and
Context goldens from its help page and instructions (or a prompt to add them)."""
from unittest.mock import patch

import pytest

from backend.datasets import autogen
from backend.datasets import goldens as goldens_store
from tests.test_app import (  # noqa: F401 - fixtures (jobs inline, no LLM rewrite)
    _create_target, app_module, client, documents_in_the_foreground)


@pytest.fixture(autouse=True)
def offline_judge(app_module, monkeypatch):
    # Never build a real judge client here: these tests must not touch the network.
    monkeypatch.setattr(app_module, "build_judge", lambda **_kw: object())


def test_prepare_gives_its_own_theme_and_asks_before_writing(client):
    bot = _create_target(client, name="Acme Support", config={"theme": "generic"})
    r = client.post(f"/api/targets/{bot['id']}/prepare-goldens", json={})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["theme"] == "acme_support"
    saved = {t["id"]: t for t in client.get("/api/targets").json()}[bot["id"]]
    assert saved["config"]["theme"] == "acme_support"
    # Nothing is written until the user says yes: they may enter their own goldens.
    assert out["seeded"] == 0 and goldens_store.load_goldens(theme="acme_support") == []
    # No help page or instructions: the app offers to learn the chatbot by asking it
    # and to look for its help pages, instead of only asking for Context goldens.
    assert out["documents"] == []
    assert out["can_generate"] == [autogen.SOURCE_CHATBOT, autogen.SOURCE_SEARCH]
    assert out["needs_context"] is False and "Generate them?" in out["message"]


def test_prepare_twice_keeps_theme_and_does_not_reseed(client):
    bot = _create_target(client, name="Twice Bot", config={"theme": "generic"})
    first = client.post(f"/api/targets/{bot['id']}/prepare-goldens", json={}).json()
    second = client.post(f"/api/targets/{bot['id']}/prepare-goldens", json={}).json()
    assert second["theme"] == first["theme"] and second["seeded"] == 0


def test_learned_goldens_replace_the_starters(app_module, client, monkeypatch):
    # Generating on save: the chatbot's own goldens fill its set, the generic starters stay out.
    monkeypatch.setattr(app_module, "find_help_pages", lambda config: [])
    monkeypatch.setattr(autogen, "interview", lambda client: [("What do you do?", "I sell shoes.")])
    learned = [{"question": "Do you sell boots?", "expected_answer": "Yes, boots and sneakers.",
                "categories": ["product"]}]
    monkeypatch.setattr(autogen, "write_dataset", lambda judge, answers, pages, persona: (learned, []))
    bot = _create_target(client, name="Shoe Shop", config={"theme": "generic"})
    out = client.post(f"/api/targets/{bot['id']}/prepare-goldens", json={"generate": True}).json()
    assert out["seeded"] == 0
    rows = goldens_store.load_goldens(theme=out["theme"])
    assert [g["question"] for g in rows] == ["Do you sell boots?"]
    assert {g["source"] for g in rows} == {autogen.GOLDEN_SOURCE}


def test_prepare_learns_from_help_page_and_instructions(app_module, client, monkeypatch, tmp_path):
    monkeypatch.setattr(app_module, "DOCUMENTS_DIR", tmp_path)
    monkeypatch.setattr(app_module, "fetch_page_text",
                        lambda url: ("Refunds take 7 business days.", "https://acme.example/faq"))
    bot = _create_target(client, name="Learner", config={
        "theme": "generic", "instructions": ["Answer in under 120 words.", "Refunds take 7 days."]})
    with patch.object(app_module, "generate_goldens_from_document", return_value=3) as gen:
        # First the user is asked: nothing is generated without their go-ahead.
        asked = client.post(f"/api/targets/{bot['id']}/prepare-goldens",
                            json={"help_url": "https://acme.example/faq"}).json()
        assert asked["documents"] == [] and gen.call_count == 0
        assert asked["can_generate"] == [autogen.SOURCE_CHATBOT, "https://acme.example/faq", "chatbot instructions"]
        assert asked["needs_context"] is False
        r = client.post(f"/api/targets/{bot['id']}/prepare-goldens", json={"generate": True})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["can_generate"] == []
    assert out["needs_context"] is False
    sources = sorted(doc["filename"] for doc in out["documents"])
    assert sources == ["chatbot instructions", "https://acme.example/faq"]
    texts = sorted(open(call.args[0], encoding="utf-8").read() for call in gen.call_args_list)
    assert any("Answer in under 120 words." in t for t in texts)
    saved = {t["id"]: t for t in client.get("/api/targets").json()}[bot["id"]]
    assert saved["config"]["help_url"] == "https://acme.example/faq"


def test_prepare_reports_a_bad_help_page_without_failing(app_module, client, monkeypatch):
    def refuse(url):
        raise ValueError("the page has almost no readable text")

    monkeypatch.setattr(app_module, "fetch_page_text", refuse)
    bot = _create_target(client, name="Bad Page", config={"theme": "generic"})
    out = client.post(f"/api/targets/{bot['id']}/prepare-goldens",
                      json={"help_url": "https://acme.example/spa", "generate": True}).json()
    assert "readable text" in " ".join(out["errors"])
    assert out["needs_context"] is True
