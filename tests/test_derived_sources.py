"""Sources the app finds itself when a chatbot's response lists none: pages the
reply links to, or passages of the theme's uploaded documents."""
import dataclasses
from unittest.mock import MagicMock

import pytest

from backend import metrics_catalog as catalog
from backend import storage
from backend.dashboard.runner import run_spec, target_features
from backend.rag import derived_sources
from backend.targets.base import ChatReply
from backend.targets.http_client import HttpTargetClient
from tests.fakes import CannedChatbot

CONFIG = {"url": "https://bot.example/chat", "body_template": '{"q": "{{message}}"}',
          "response_path": "reply"}


def _db(tmp_path):
    conn = storage.init_db(str(tmp_path / "t.db"))
    return conn, storage.add_target(conn, "bot", "http", {})


def _recording_metric(score=0.9):
    metric = MagicMock()
    metric.cases = []

    def measure(case):
        metric.cases.append(case)
        metric.score, metric.reason = score, "ok"

    metric.measure.side_effect = measure
    metric.is_successful.side_effect = lambda: metric.score >= 0.5
    return metric


def _with(spec, metric, items):
    copy = dataclasses.replace(spec)
    copy.build_metric = lambda judge, threshold=0.7, **kw: metric
    copy.cases = lambda **_kw: items
    return copy


class LinkBot(CannedChatbot):
    """Answers with a fixed reply and reports no sources of its own."""

    def __init__(self, reply, fallback, retrieval=None):
        super().__init__()
        self.text, self.sources_fallback, self.retrieval = reply, fallback, retrieval
        self.context_path = "sources" if retrieval else ""
        self.tools_path = ""

    def chat(self, message, history=None):
        return ChatReply(reply=self.text, model="canned", mode="http", retrieval_context=self.retrieval)


@pytest.fixture
def kb(tmp_path, monkeypatch):
    """A theme folder with two documents, one also saved as enhanced text."""
    root = tmp_path / "documents"
    theme = root / "shop"
    theme.mkdir(parents=True)
    (theme / "1_refunds.txt").write_text(
        "Refunds are processed within 7 business days.\n\nDigital goods are non-refundable.",
        encoding="utf-8")
    (theme / "2_shipping.txt").write_text("raw shipping notes", encoding="utf-8")
    (theme / "2_shipping_enhanced.txt").write_text(
        "Express shipping costs $9.99 and takes 2-3 business days.", encoding="utf-8")
    monkeypatch.setattr(derived_sources, "DOCUMENTS_DIR", root)
    derived_sources._CHUNKS.clear()
    return theme


# --- finding links ------------------------------------------------------------
def test_cited_urls_reads_links_from_markdown_and_plain_text():
    reply = ("See [our policy](https://shop.example/refunds). Also https://shop.example/ship, "
             "and again https://shop.example/refunds.")
    assert derived_sources.cited_urls(reply) == [
        "https://shop.example/refunds", "https://shop.example/ship"]


def test_cited_urls_keeps_at_most_max_links():
    reply = " ".join(f"https://a.example/{i}" for i in range(6))
    assert len(derived_sources.cited_urls(reply)) == derived_sources.MAX_LINKS


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:8765/api/targets", "http://localhost/x", "http://10.0.0.5/",
    "http://192.168.1.1/", "http://169.254.169.254/latest", "ftp://shop.example/file",
])
def test_private_and_non_web_addresses_are_never_fetched(url, monkeypatch):
    get = MagicMock()
    monkeypatch.setattr(derived_sources.requests, "get", get)
    assert derived_sources.cited_pages(f"Read {url} please") == []
    get.assert_not_called()


def test_cited_pages_fetches_each_link_as_text(monkeypatch):
    monkeypatch.setattr(derived_sources, "_public_host", lambda host: True)
    response = MagicMock(ok=True, status_code=200, is_redirect=False, encoding="utf-8",
                         headers={"Content-Type": "text/html"})
    response.iter_content.return_value = [b"<html><nav>menu</nav><p>Refunds take 7 days.</p></html>"]
    monkeypatch.setattr(derived_sources.requests, "get", MagicMock(return_value=response))
    pages = derived_sources.cited_pages("Details: https://shop.example/refunds")
    assert pages == ["Source: https://shop.example/refunds\nRefunds take 7 days."]


def test_a_redirect_to_a_private_address_is_not_followed(monkeypatch):
    monkeypatch.setattr(derived_sources, "_public_host", lambda host: host != "127.0.0.1")
    redirect = MagicMock(ok=False, status_code=302, is_redirect=True,
                         headers={"Location": "http://127.0.0.1:8765/api/targets"})
    get = MagicMock(return_value=redirect)
    monkeypatch.setattr(derived_sources.requests, "get", get)
    assert derived_sources.cited_pages("https://shop.example/r") == []
    assert get.call_count == 1


# --- knowledge base -------------------------------------------------------------
def test_knowledge_base_returns_the_best_matching_passages(kb):
    passages = derived_sources.knowledge_base("shop", "How long do refunds take?",
                                              "Refunds take 7 business days.")
    assert passages[0].startswith("[1_refunds.txt]")
    assert "7 business days" in passages[0]


def test_knowledge_base_uses_the_enhanced_text_instead_of_the_original(kb):
    passages = derived_sources.knowledge_base("shop", "How much is express shipping?", "$9.99")
    assert any("2_shipping_enhanced.txt" in p for p in passages)
    assert not any("raw shipping notes" in p for p in passages)


def test_knowledge_base_is_empty_without_documents_or_matches(kb):
    assert derived_sources.knowledge_base("other_theme", "refunds?", "7 days") == []
    assert derived_sources.knowledge_base("shop", "weather today?", "Sunny.") == []


# --- target config --------------------------------------------------------------
def test_http_client_validates_and_keeps_sources_fallback():
    assert HttpTargetClient(CONFIG).sources_fallback == ""
    assert HttpTargetClient({**CONFIG, "sources_fallback": "knowledge_base"}).sources_fallback == "knowledge_base"
    with pytest.raises(ValueError):
        HttpTargetClient({**CONFIG, "sources_fallback": "guess"})


def test_features_count_a_fallback_only_for_metrics_that_do_not_grade_the_retriever():
    bot = LinkBot("r", "knowledge_base")
    assert target_features(bot, catalog.get_spec("faithfulness"))["context_path"] is True
    assert target_features(bot, catalog.get_spec("contextual_precision"))["context_path"] is False
    assert target_features(LinkBot("r", ""), catalog.get_spec("faithfulness"))["context_path"] is False


def test_retriever_metrics_are_flagged_in_the_catalog():
    graded = {s.key for s in catalog.all_specs() if s.grades_retriever}
    assert graded == {"contextual_precision", "contextual_recall", "contextual_relevancy",
                      "turn_contextual_precision", "turn_contextual_recall",
                      "turn_contextual_relevancy"}


# --- runs -------------------------------------------------------------------------
GOLDEN = {"id": "g1", "question": "How long do refunds take?", "expected_answer": "7 business days",
          "context": ["golden fact"]}


def test_faithfulness_scores_the_knowledge_base_passages_and_says_so(tmp_path, kb):
    conn, target_id = _db(tmp_path)
    metric = _recording_metric()
    spec = _with(catalog.get_spec("faithfulness"), metric, [GOLDEN])
    result = run_spec(spec, judge=object(), target=LinkBot("Refunds take 7 business days.", "knowledge_base"),
                      target_id=target_id, conn=conn, theme="shop")
    assert result["status"] == "pass", result
    assert "7 business days" in metric.cases[0].retrieval_context[0]
    assert result["rows"][0]["context_source"] == "knowledge_base"
    assert "knowledge base" in result["note"].lower()


def test_cited_links_feed_citation_quality(tmp_path, monkeypatch):
    monkeypatch.setattr(derived_sources, "cited_pages",
                        lambda reply: ["Source: https://shop.example/r\nRefunds take 7 days."])
    conn, target_id = _db(tmp_path)
    metric = _recording_metric()
    spec = _with(catalog.get_spec("citation_quality"), metric, [GOLDEN])
    bot = LinkBot("7 days, see https://shop.example/r", "cited_links")
    result = run_spec(spec, judge=object(), target=bot, target_id=target_id, conn=conn)
    assert result["status"] == "pass", result
    assert result["rows"][0]["context_source"] == "cited_pages"
    assert metric.cases[0].retrieval_context == ["Source: https://shop.example/r\nRefunds take 7 days."]


def test_no_passage_found_falls_back_to_the_golden_context(tmp_path, kb):
    conn, target_id = _db(tmp_path)
    metric = _recording_metric()
    spec = _with(catalog.get_spec("faithfulness"), metric, [GOLDEN])
    result = run_spec(spec, judge=object(), target=LinkBot("Sunny weather.", "knowledge_base"),
                      target_id=target_id, conn=conn, theme="other_theme")
    assert result["rows"][0]["context_source"] == "golden"
    assert metric.cases[0].retrieval_context == ["golden fact"]


def test_no_passage_and_no_golden_context_skips_the_case(tmp_path, kb):
    conn, target_id = _db(tmp_path)
    metric = _recording_metric()
    golden = {k: v for k, v in GOLDEN.items() if k != "context"}
    spec = _with(catalog.get_spec("citation_quality"), metric, [golden])
    result = run_spec(spec, judge=object(), target=LinkBot("No links here.", "cited_links"),
                      target_id=target_id, conn=conn)
    assert metric.cases == []
    assert result["status"] == "error" and "link" in result["error"].lower()


def test_retriever_metrics_ignore_the_fallback(tmp_path, kb):
    conn, target_id = _db(tmp_path)
    metric = _recording_metric()
    spec = _with(catalog.get_spec("contextual_relevancy"), metric, [GOLDEN])
    result = run_spec(spec, judge=object(), target=LinkBot("Refunds take 7 business days.", "knowledge_base"),
                      target_id=target_id, conn=conn, theme="shop")
    assert result["rows"][0]["context_source"] == "golden"
    assert metric.cases[0].retrieval_context == ["golden fact"]


def test_the_chatbots_own_sources_win_over_the_fallback(tmp_path, kb):
    conn, target_id = _db(tmp_path)
    metric = _recording_metric()
    spec = _with(catalog.get_spec("faithfulness"), metric, [GOLDEN])
    bot = LinkBot("Refunds take 7 business days.", "knowledge_base", retrieval=["own doc"])
    result = run_spec(spec, judge=object(), target=bot, target_id=target_id, conn=conn, theme="shop")
    assert result["rows"][0]["context_source"] == "chatbot"
    assert metric.cases[0].retrieval_context == ["own doc"]


SCENARIO = {"id": "s1", "name": "refunds", "user_turns": ["How long do refunds take?"],
            "expected_outcome": "explains refunds"}


def test_turn_faithfulness_gets_knowledge_base_passages_per_turn(tmp_path, kb):
    conn, target_id = _db(tmp_path)
    metric = _recording_metric()
    spec = _with(catalog.get_spec("turn_faithfulness"), metric, [SCENARIO])
    result = run_spec(spec, judge=object(), target=LinkBot("Refunds take 7 business days.", "knowledge_base"),
                      target_id=target_id, conn=conn, theme="shop")
    assert result["status"] == "pass", result
    assistant = [t for t in metric.cases[0].turns if t.role == "assistant"]
    assert "7 business days" in assistant[0].retrieval_context[0]
    assert result["rows"][0]["context_source"] == "knowledge_base"


# --- what the UI is told --------------------------------------------------------
@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("JUDGE_DB_PATH", str(tmp_path / "app_test.db"))
    monkeypatch.setenv("JUDGE_API_KEY", "fake-key")
    monkeypatch.setenv("JUDGE_START_JOB_WORKER", "0")
    from fastapi.testclient import TestClient

    from backend.dashboard import app as app_module

    monkeypatch.setattr(app_module, "_conn", storage.init_db(str(tmp_path / "api.db")))
    return TestClient(app_module.app, base_url="http://127.0.0.1"), app_module


def _metrics_for(api, **config):
    client, app_module = api
    target_id = storage.add_target(app_module._conn, "bot", "http", {**CONFIG, "theme": "generic", **config})
    return {m["key"]: m for m in client.get(f"/api/metrics?target_id={target_id}").json()}


def test_metrics_endpoint_turns_on_answer_metrics_with_a_fallback_but_not_retriever_ones(api):
    plain = _metrics_for(api)
    fallback = _metrics_for(api, sources_fallback="knowledge_base")
    assert plain["citation_quality"]["unavailable"]
    assert fallback["citation_quality"]["unavailable"] is None
    assert plain["turn_faithfulness"]["unavailable"]
    assert fallback["turn_faithfulness"]["unavailable"] is None
    assert fallback["turn_contextual_relevancy"]["unavailable"] == plain["turn_contextual_relevancy"]["unavailable"]


def test_saving_a_target_rejects_an_unknown_sources_fallback(api):
    client, _ = api
    bad = client.post("/api/targets", json={"name": "b", "config": {**CONFIG, "sources_fallback": "guess"}})
    assert bad.status_code == 400
    good = client.post("/api/targets", json={"name": "b", "config": {**CONFIG, "sources_fallback": "cited_links"}})
    assert good.json()["config"]["sources_fallback"] == "cited_links"
