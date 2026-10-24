"""Goldens without context (question + expected answer only), made the way
DeepEval makes them: Synthesizer.generate_goldens_from_scratch writes the
questions from a StylingConfig (scenario, task, input format), and DeepEval's
own expected-output prompt (SynthesizerTemplate.generate_synthetic_expected_output)
writes each answer. The judge is the Synthesizer's model, so no second key.

Reference-free metrics such as Self-Check Consistency run only on these.
No embedder is involved, so this never imports sentence-transformers.
"""
from __future__ import annotations

from deepeval.synthesizer import Synthesizer
from deepeval.synthesizer.config import StylingConfig
from deepeval.synthesizer.templates.template import SynthesizerTemplate

from backend.datasets import goldens as goldens_store

SOURCE = "deepeval_scratch"
NOTHING_KNOWN = "Nothing is known about the chatbot beyond the question."
DEFAULT_COUNT = 5
MAX_COUNT = 20
# Known answers of the golden set given to the expected-output prompt, at most.
KNOWN_ANSWERS = 8
# Characters of that knowledge put in the scenario, so questions are about its topics.
SCENARIO_CHARS = 800


def styling_for(name: str, persona: str = "", about: str = "") -> StylingConfig:
    """The StylingConfig generate_goldens_from_scratch requires (scenario, task,
    input_format), plus the expected answers' format. about (what the golden set
    already says about the chatbot) keeps the questions on its topics."""
    who = persona.strip() or f"the chatbot '{name}'"
    known = f" What it is known to cover:\n{about[:SCENARIO_CHARS]}" if about else ""
    return StylingConfig(
        scenario=f"Real users chatting with {who} to get help with what it covers; they ask about "
                 f"its topics, not about the chatbot itself.{known}",
        task=f"Answer users' questions as {who}.",
        input_format="One question a real user would type to the chatbot, in plain words, with no "
                     "background or context attached.",
        expected_output_format="A short, direct answer that a good reply from this chatbot would give.",
    )


def _about(theme: str, persona: str) -> str:
    """What is known about the chatbot, as the expected-output prompt's context:
    its role and answers already in its golden set. Not stored on the goldens."""
    lines = [f"The chatbot's role: {persona.strip()}"] if persona.strip() else []
    known = [g for g in goldens_store.load_goldens(theme=theme)
             if g.get("expected_answer") and g.get("source") != SOURCE]
    lines += [f"Q: {g['question']}\nA: {g['expected_answer']}" for g in known[:KNOWN_ANSWERS]]
    return "\n".join(lines) or NOTHING_KNOWN


def generate_goldens_from_scratch(theme: str, judge, name: str, persona: str = "",
                                  count: int = DEFAULT_COUNT, review: bool = False) -> list[dict]:
    """Adds count goldens with no context to theme and returns them. Raises on
    any Synthesizer failure (the caller reports it)."""
    count = max(1, min(MAX_COUNT, int(count)))
    about = _about(theme, persona)
    known = about if about != NOTHING_KNOWN else ""
    styling = styling_for(name, persona, known)
    synthesizer = Synthesizer(model=judge, async_mode=False, styling_config=styling)
    generated = synthesizer.generate_goldens_from_scratch(num_goldens=count)[:count]
    added = []
    for golden in generated:
        question = (golden.input or "").strip()
        if not question:
            continue
        expected = synthesizer._generate(SynthesizerTemplate.generate_synthetic_expected_output(
            input=question, context=about, expected_output_format=styling.expected_output_format))
        added.append(goldens_store.add_golden(
            theme=theme, question=question, expected_answer=str(expected or "").strip(), context=[],
            categories=[], source=SOURCE,
            extra={"status": "draft"} if review else None,
        ))
    return added
