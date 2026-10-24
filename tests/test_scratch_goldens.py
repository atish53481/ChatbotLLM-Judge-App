"""Goldens without context made with DeepEval's generate_goldens_from_scratch."""
from types import SimpleNamespace
from unittest.mock import patch

from backend.datasets import goldens as goldens_store
from backend.rag import scratch


class FakeSynthesizer:
    def __init__(self, model, async_mode, styling_config):
        self.styling = styling_config
        self.prompts = []

    def generate_goldens_from_scratch(self, num_goldens):
        return [SimpleNamespace(input=f"Question {i}?") for i in range(num_goldens)] + [SimpleNamespace(input=" ")]

    def _generate(self, prompt):
        self.prompts.append(prompt)
        return "An answer."


def test_scratch_goldens_have_question_and_answer_but_no_context():
    with patch.object(scratch, "Synthesizer", FakeSynthesizer):
        added = scratch.generate_goldens_from_scratch("scratch_theme", object(), "Shop Bot",
                                                      persona="A shop's support bot", count=2)
    assert [g["question"] for g in added] == ["Question 0?", "Question 1?"]
    stored = goldens_store.load_goldens(theme="scratch_theme")
    assert all(g["context"] == [] and g["expected_answer"] == "An answer." for g in stored)
    assert all(g["source"] == scratch.SOURCE for g in stored)


def test_styling_names_the_chatbot_role():
    styling = scratch.styling_for("Shop Bot", "A shop's support bot")
    assert styling.scenario and styling.task and styling.input_format  # DeepEval requires all three
    assert "A shop's support bot" in styling.task
    assert "Shop Bot" in scratch.styling_for("Shop Bot").task


def test_scenario_carries_what_the_golden_set_knows():
    goldens_store.add_golden("known_theme", "Which mugs?", "The 12oz ceramic mug.", [])
    seen = []

    class Recording(FakeSynthesizer):
        def __init__(self, model, async_mode, styling_config):
            super().__init__(model, async_mode, styling_config)
            seen.append(styling_config)

    with patch.object(scratch, "Synthesizer", Recording):
        scratch.generate_goldens_from_scratch("known_theme", object(), "Bot", count=1)
    assert "12oz ceramic mug" in seen[0].scenario
