"""DeepEval's RAGAS metric (deepeval.metrics.ragas.RagasMetric) run on the app's judge.

RagasMetric drives the ragas library through LangChain: it takes a LangChain chat
model and, for its Answer Relevancy part, LangChain embeddings. JudgeChatModel
hands every ragas prompt to the app's judge (GroqJudge), so its calls keep the
judge's call cap, retries, 90 s timeout, token counter and the Stop button.
LocalEmbeddings uses the same local sentence-transformers model as document
generation, so no OpenAI key is needed.

ragas is optional: it is not in requirements.txt because its dependency
`instructor` pins jiter<0.15, which forces openai back from the 3.4x line.
missing_reason() says what to install; the metric is unavailable until then.
"""
from __future__ import annotations

import importlib.util
import math
from typing import Any

from langchain_core.embeddings import Embeddings
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult

RAGAS_PACKAGES = ("ragas", "instructor", "datasets")
INSTALL_HINT = (
    "Needs the optional ragas package: run "
    "`.venv\\Scripts\\python -m pip install ragas instructor \"datasets<5\"` and restart the backend "
    "(datasets 5 pulls huggingface_hub 2, which breaks transformers and DeepEval's click pin). "
    "Note: instructor (required by ragas) caps jiter<0.15, so pip moves openai back to an "
    "older 3.x release; update-packages.bat then keeps it there."
)
# The five ragas scores RagasMetric averages (its score_breakdown keys).
COMBINES = (
    "Contextual Precision (ragas)",
    "Contextual Recall (ragas)",
    "Context Entities Recall (ragas)",
    "Answer Relevancy (ragas)",
    "Faithfulness (ragas)",
)


def missing_reason() -> str | None:
    """None when ragas and what it imports are installed, else what to install."""
    missing = [name for name in RAGAS_PACKAGES if importlib.util.find_spec(name) is None]
    return f"{INSTALL_HINT} Missing: {', '.join(missing)}." if missing else None


def _text(message: BaseMessage) -> str:
    content = message.content
    if isinstance(content, str):
        return content
    return "\n".join(part.get("text", "") if isinstance(part, dict) else str(part) for part in content)


class JudgeChatModel(BaseChatModel):
    """A LangChain chat model whose every call is one call to the app's judge."""

    judge: Any
    temperature: float = 0.0

    @property
    def _llm_type(self) -> str:
        return "llm-judge"

    def _generate(self, messages: list[BaseMessage], stop=None, run_manager=None, **kwargs) -> ChatResult:
        prompt = "\n\n".join(_text(m) for m in messages)
        output = self.judge.generate(prompt)
        text = output[0] if isinstance(output, tuple) else output
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=str(text)))])


class LocalEmbeddings(Embeddings):
    """LangChain embeddings over the app's local sentence-transformers model,
    imported on first use (sentence_transformers takes ~40 s to import)."""

    def __init__(self):
        self._embedder = None

    def _model(self):
        if self._embedder is None:
            from backend.rag.embeddings import LocalSentenceEmbedder
            self._embedder = LocalSentenceEmbedder()
        return self._embedder

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._model().embed_texts(list(texts))

    def embed_query(self, text: str) -> list[float]:
        return self._model().embed_text(text)


def _is_score(value) -> bool:
    """A real number; ragas returns NaN when it could not parse the judge's
    answer or a call timed out."""
    try:
        return not math.isnan(float(value))
    except (TypeError, ValueError):
        return False


def build_ragas_metric(judge, threshold: float):
    """DeepEval's RagasMetric on the judge; its reason lists the five sub-scores.

    DeepEval's own measure() averages all five, so a single NaN sub-score made
    the whole score NaN (and crashed the run's stats). This one averages the
    sub-scores ragas did produce, names the ones it could not, and raises when
    none came back, so the case is an error instead of a NaN score."""
    from deepeval.metrics import ragas as de_ragas

    class _RagasWithReason(de_ragas.RagasMetric):
        def measure(self, test_case, *args, **kwargs):
            parts = [
                de_ragas.RAGASContextualPrecisionMetric(model=self.model, _track=False),
                de_ragas.RAGASContextualRecallMetric(model=self.model, _track=False),
                de_ragas.RAGASContextualEntitiesRecall(model=self.model, _track=False),
                de_ragas.RAGASAnswerRelevancyMetric(model=self.model, embeddings=self.embeddings,
                                                    _track=False),
                de_ragas.RAGASFaithfulnessMetric(model=self.model, _track=False),
            ]
            breakdown: dict[str, float | None] = {}
            for part in parts:
                value = part.measure(test_case)
                breakdown[part.__name__] = float(value) if _is_score(value) else None
            scored = [v for v in breakdown.values() if v is not None]
            missing = [name.replace(" (ragas)", "") for name, v in breakdown.items() if v is None]
            if not scored:
                raise ValueError("ragas returned no score for any of its five parts "
                                 "(the judge's answers could not be parsed or timed out)")
            self.score = sum(scored) / len(scored)
            self.success = self.score >= self.threshold
            self.score_breakdown = {k: v for k, v in breakdown.items() if v is not None}
            shown = [f"{name.replace(' (ragas)', '')} {v:.2f}" for name, v in self.score_breakdown.items()]
            self.reason = "RAGAS = average of " + ", ".join(shown)
            if missing:
                self.reason += f" (no score from ragas for: {', '.join(missing)})"
            return self.score

    return _RagasWithReason(threshold=threshold, model=JudgeChatModel(judge=judge),
                            embeddings=LocalEmbeddings())
