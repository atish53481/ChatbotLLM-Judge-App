"""Generates a golden dataset from an uploaded reference document, using
DeepEval's own Synthesizer — not a custom retrieval pipeline. See
docs/superpowers/specs/2026-09-17-document-golden-generation-design.md.
"""
from __future__ import annotations

from deepeval.synthesizer import Synthesizer
from deepeval.synthesizer import Evolution
from deepeval.synthesizer.config import (
    ContextConstructionConfig,
    EvolutionConfig,
    FiltrationConfig,
    StylingConfig,
)

from backend.datasets import goldens as goldens_store
from backend.rag.embeddings import LocalSentenceEmbedder
from backend.rag.generation_options import EVOLUTION_NAMES, STYLING_FIELDS


# Evol-Instruct rewrites that make generated questions harder or broader:
# key the API takes -> DeepEval's Evolution.
EVOLUTIONS = {
    "reasoning": Evolution.REASONING,
    "multi_context": Evolution.MULTICONTEXT,
    "concretizing": Evolution.CONCRETIZING,
    "constrained": Evolution.CONSTRAINED,
    "comparative": Evolution.COMPARATIVE,
    "hypothetical": Evolution.HYPOTHETICAL,
    "in_breadth": Evolution.IN_BREADTH,
}
assert tuple(EVOLUTIONS) == EVOLUTION_NAMES


def synthesizer_configs(options: dict | None, judge) -> dict:
    """Synthesizer keyword arguments for the optional generation settings.
    Empty options give {} - DeepEval's defaults, today's behaviour exactly.
    Raises ValueError for an unknown evolution."""
    options = options or {}
    configs: dict = {}
    names = [n for n in options.get("evolutions") or [] if n]
    unknown = [n for n in names if n not in EVOLUTIONS]
    if unknown:
        raise ValueError(f"unknown evolution {', '.join(unknown)}: use {', '.join(EVOLUTIONS)}")
    if names or options.get("num_evolutions"):
        chosen = names or list(EVOLUTIONS)
        configs["evolution_config"] = EvolutionConfig(
            num_evolutions=max(1, min(5, int(options.get("num_evolutions") or 1))),
            evolutions={EVOLUTIONS[n]: 1 / len(chosen) for n in chosen},
        )
    if options.get("quality_threshold") is not None or options.get("max_retries") is not None:
        configs["filtration_config"] = FiltrationConfig(
            synthetic_input_quality_threshold=float(options.get("quality_threshold", 0.5)),
            max_quality_retries=int(options.get("max_retries", 3)),
            critic_model=judge,
        )
    styling = {k: str(options[k]).strip() for k in STYLING_FIELDS if str(options.get(k) or "").strip()}
    if styling:
        configs["styling_config"] = StylingConfig(**styling)
    return configs


def _evolutions_of(golden) -> list[str]:
    meta = getattr(golden, "additional_metadata", None)
    value = meta.get("evolutions") if isinstance(meta, dict) else None
    return [str(e.value if hasattr(e, "value") else e) for e in value] if isinstance(value, list) else []


def generate_goldens_from_document(
    path: str, theme: str, filename: str, judge, document_id: int | None = None,
    options: dict | None = None,
) -> int:
    """Runs the Synthesizer on one document and saves what it produces as
    goldens under theme, tagged with their source document. Returns the
    count of goldens created. Raises on any Synthesizer/parsing failure —
    the caller (the /api/documents endpoint) is responsible for catching it
    and recording a document status of "error".

    options (all optional): evolutions, num_evolutions, quality_threshold,
    max_retries, the StylingConfig fields, and review (save the goldens as
    drafts that runs skip until accepted)."""
    options = options or {}
    synthesizer = Synthesizer(model=judge, **synthesizer_configs(options, judge))
    # critic_model must be set explicitly: ContextConstructionConfig resolves
    # it in __post_init__ (construction time), before Synthesizer ever gets a
    # chance to backfill a None with its own model — an unset critic_model
    # otherwise silently tries to build a default OpenAI model and needs
    # OPENAI_API_KEY, defeating the "reuse the judge, no second key" goal.
    config = ContextConstructionConfig(embedder=LocalSentenceEmbedder(), critic_model=judge)
    generated = synthesizer.generate_goldens_from_docs(
        document_paths=[path],
        context_construction_config=config,
    )
    for golden in generated:
        goldens_store.add_golden(
            theme=theme,
            question=golden.input,
            expected_answer=golden.expected_output or "",
            context=golden.context or [],
            categories=[],
            source="synthesized",
            source_document=filename,
            source_document_id=document_id,
            extra={"evolutions": _evolutions_of(golden),
                   **({"status": "draft"} if options.get("review") else {})},
        )
    return len(generated)
