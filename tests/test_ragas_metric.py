"""DeepEval's RAGAS metric (https://deepeval.com/docs/metrics-ragas), offline:
no ragas package, no judge tokens."""
import importlib.util

import pytest

from backend import metrics_catalog as mc
from backend.judges import ragas_adapter
from tests.test_app import app_module, client  # noqa: F401 - fixtures


class EchoJudge:
    def __init__(self):
        self.prompts = []

    def generate(self, prompt, *args, **kwargs):
        self.prompts.append(prompt)
        return '{"verdict": 1}', 0.0


def test_ragas_spec_matches_deepeval_docs():
    spec = mc.get_spec("ragas")
    assert spec.title == "RAGAS" and spec.threshold == 0.3  # DeepEval's default threshold
    assert set(spec.scores_on) == {"input", "actual_output", "expected_output", "retrieval_context"}
    assert spec.dataset_name == "goldens_with_context"
    assert spec.opt_in is True
    assert spec.combines == ("Contextual Precision", "Contextual Recall", "Context Entities Recall",
                             "Answer Relevancy", "Faithfulness")
    assert spec.ui_category == "retrieval" and spec.question and spec.scale_hint


def test_ragas_case_carries_expected_output_and_retrieval_context():
    golden = {"id": "g1", "question": "Return window?", "expected_answer": "30 days.",
              "context": ["Returns accepted within 30 days."]}
    case = mc.get_spec("ragas").build_case(golden, "You have 30 days.", ["Returns: 30 days."])
    assert case.expected_output == "30 days."
    assert case.retrieval_context == ["Returns: 30 days."]


def test_ragas_is_unavailable_without_the_package(monkeypatch):
    real = importlib.util.find_spec
    monkeypatch.setattr(ragas_adapter.importlib.util, "find_spec",
                        lambda name, *a: None if name == "ragas" else real(name, *a))
    reason = mc.get_spec("ragas").applies({}, [])
    assert reason and 'pip install ragas instructor "datasets<5"' in reason and "Missing: ragas" in reason


def test_ragas_is_available_when_installed(monkeypatch):
    monkeypatch.setattr(ragas_adapter.importlib.util, "find_spec", lambda name, *a: object())
    assert mc.get_spec("ragas").applies({}, []) is None


def test_judge_chat_model_sends_every_prompt_to_the_app_judge():
    judge = EchoJudge()
    model = ragas_adapter.JudgeChatModel(judge=judge)
    reply = model.invoke("Is the claim supported?")
    assert reply.content == '{"verdict": 1}'
    assert judge.prompts == ["Is the claim supported?"]


@pytest.mark.skipif(importlib.util.find_spec("ragas") is None, reason="ragas not installed")
def test_build_ragas_metric_uses_the_judge():
    metric = mc.get_spec("ragas").build_metric(EchoJudge(), threshold=0.4)
    assert metric.threshold == 0.4
    assert isinstance(metric.model, ragas_adapter.JudgeChatModel)
    assert isinstance(metric.embeddings, ragas_adapter.LocalEmbeddings)


def test_ragas_lists_its_cases_while_the_package_is_missing(client, monkeypatch):
    real = importlib.util.find_spec
    monkeypatch.setattr(ragas_adapter.importlib.util, "find_spec",
                        lambda name, *a: None if name == "ragas" else real(name, *a))
    rows = {m["key"]: m for m in client.get("/api/metrics").json()}
    ragas, faithfulness = rows["ragas"], rows["faithfulness"]
    assert ragas["unavailable"] and "pip install ragas" in ragas["unavailable"]
    # Same golden set as Faithfulness (goldens with context): the count is not hidden.
    assert ragas["cases_available"] == faithfulness["cases_available"] > 0
    listed = client.get("/api/metrics/ragas/cases").json()
    assert len(listed["cases"]) == ragas["cases_available"]
    assert ragas["opt_in"] is True and len(ragas["combines"]) == 5


_PARTS = ("RAGASContextualPrecisionMetric", "RAGASContextualRecallMetric",
          "RAGASContextualEntitiesRecall", "RAGASAnswerRelevancyMetric", "RAGASFaithfulnessMetric")


def _fake_parts(monkeypatch, scores):
    """Each ragas part returns the next score instead of calling the judge."""
    from deepeval.metrics import ragas as de_ragas
    for name, value in zip(_PARTS, scores):
        monkeypatch.setattr(getattr(de_ragas, name), "measure", lambda self, tc, v=value: v)


@pytest.mark.skipif(importlib.util.find_spec("ragas") is None, reason="ragas not installed")
def test_ragas_averages_only_the_parts_ragas_scored(monkeypatch):
    _fake_parts(monkeypatch, [1.0, float("nan"), 0.5, 0.0, float("nan")])
    metric = mc.get_spec("ragas").build_metric(EchoJudge(), threshold=0.3)
    assert metric.measure(object()) == pytest.approx(0.5)
    assert metric.success is True
    assert "no score from ragas for: Contextual Recall, Faithfulness" in metric.reason


@pytest.mark.skipif(importlib.util.find_spec("ragas") is None, reason="ragas not installed")
def test_ragas_with_no_part_scored_is_a_case_error(monkeypatch):
    _fake_parts(monkeypatch, [float("nan")] * 5)
    metric = mc.get_spec("ragas").build_metric(EchoJudge(), threshold=0.3)
    with pytest.raises(ValueError, match="no score"):
        metric.measure(object())


def test_summary_stats_ignores_nan():
    from backend.dashboard.stats import summary_stats
    stats = summary_stats([0.8, float("nan"), None])
    assert stats["n"] == 1 and stats["mean"] == 0.8
    assert summary_stats([float("nan")]) is None
