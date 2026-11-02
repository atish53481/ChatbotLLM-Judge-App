from backend.metric_advice import ADVICE, DEEPTEAM_ADVICE
from backend.metrics_catalog import ALL_SPECS, DEEPTEAM_SPECS


def test_every_metric_says_how_to_improve_it():
    deepteam = {s.key for s in DEEPTEAM_SPECS}
    assert set(ADVICE) == {s.key for s in ALL_SPECS} - deepteam
    assert all(s.improve is DEEPTEAM_ADVICE for s in DEEPTEAM_SPECS)  # they share one plan
    for spec in ALL_SPECS:
        advice = spec.improve
        # Both sides: what to change in the chatbot, and what to check in the test itself.
        assert advice["chatbot"] and advice["tests"], spec.key
        assert all(isinstance(step, str) and step.strip().endswith(".") for step in advice["chatbot"] + advice["tests"]), spec.key
