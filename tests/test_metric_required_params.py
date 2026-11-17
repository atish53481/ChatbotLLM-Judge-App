"""scores_on (shown in the side panel's metric hover card as "Needed" fields)
must list every test-case field DeepEval itself requires for that metric."""
import inspect

from backend import metrics_catalog as mc


def _deepeval_class(spec):
    fn = spec.build_metric
    for cell in fn.__closure__ or ():
        value = cell.cell_contents
        if inspect.isclass(value) and isinstance(getattr(value, "_required_params", None), list):
            return value
    for name in fn.__code__.co_names:
        value = getattr(mc, name, None)
        if inspect.isclass(value) and isinstance(getattr(value, "_required_params", None), list):
            return value
    return None


# "Required Arguments" of each metric's page on deepeval.com/docs (checked 2026-11-14).
# Answer Relevancy is referenceless: no expected_output.
DOCS_REQUIRED = {
    "answer_relevancy": {"input", "actual_output"},
    "faithfulness": {"input", "actual_output", "retrieval_context"},
    "contextual_precision": {"input", "actual_output", "expected_output", "retrieval_context"},
    "contextual_recall": {"input", "actual_output", "expected_output", "retrieval_context"},
    "contextual_relevancy": {"input", "actual_output", "retrieval_context"},
    "hallucination": {"input", "actual_output", "context"},
    "summarization": {"input", "actual_output"},
    "bias": {"input", "actual_output"},
    "toxicity": {"input", "actual_output"},
    "tool_correctness": {"input", "actual_output", "tools_called", "expected_tools"},
    "prompt_alignment": {"input", "actual_output"},
    "argument_correctness": {"input", "actual_output", "tools_called"},
    "ragas": {"input", "actual_output", "expected_output", "retrieval_context"},
    # Multi-turn (ConversationalTestCase): turns always; extras per metric.
    "conversation_completeness": {"turns"},
    "knowledge_retention": {"turns"},
    "turn_relevancy": {"turns"},
    "role_adherence": {"turns", "chatbot_role"},
    "turn_faithfulness": {"turns", "retrieval_context"},
    "turn_contextual_relevancy": {"turns", "retrieval_context"},
    "turn_contextual_precision": {"turns", "retrieval_context", "expected_outcome"},
    "turn_contextual_recall": {"turns", "retrieval_context", "expected_outcome"},
}


def test_scores_on_matches_deepeval_docs():
    specs = {s.key: s for s in mc.all_specs()}
    wrong = {key: sorted(set(specs[key].scores_on) ^ need)
             for key, need in DOCS_REQUIRED.items() if set(specs[key].scores_on) != need}
    assert wrong == {}


def test_scores_on_covers_deepeval_required_params():
    missing = {}
    for spec in mc.all_specs():
        cls = _deepeval_class(spec)
        if cls is None:
            continue
        required = {getattr(p, "value", str(p)) for p in cls._required_params}
        gap = required - set(spec.scores_on)
        if gap:
            missing[spec.key] = sorted(gap)
    assert missing == {}


def test_security_probes_list_tools_as_optional():
    for spec in mc.all_specs():
        if spec.reads_reported_tools:
            assert spec.optional_on == ("tools_called",), spec.key
            assert set(spec.scores_on) == {"input", "actual_output", "context"}, spec.key


def test_deepteam_specs_name_their_deepteam_metric():
    for vuln in mc.deepteam_sync.new_vulnerabilities():
        assert vuln["class"] in mc.DEEPTEAM_METRIC_CLASSES, vuln["class"]
    assert mc.DEEPTEAM_AGENT_FIELDS <= set(mc.DEEPTEAM_METRIC_CLASSES)
    assert mc._deepteam_fields("BOLA") == ("input", "actual_output", "tools_called", "retrieval_context")
    assert mc._deepteam_fields("Toxicity") == ("input", "actual_output")


def test_deepteam_mapping_matches_installed_package(monkeypatch):
    """Each vulnerability's metric class and the fields it reads, as DeepTeam itself reports."""
    import importlib
    import json
    import os
    import re

    monkeypatch.setenv("DEEPTEAM_TELEMETRY_OPT_OUT", "YES")
    monkeypatch.setenv("OPENAI_API_KEY", os.environ.get("OPENAI_API_KEY") or "sk-test")
    try:
        vulns = importlib.import_module("deepteam.vulnerabilities")
    except Exception:
        import pytest
        pytest.skip("deepteam not installed")
    with open(mc.deepteam_sync.DEFAULT_CATALOG_PATH, encoding="utf-8") as fh:
        catalog = json.load(fh)
    for row in catalog["vulnerabilities"]:
        vuln = getattr(vulns, row["class"])()
        metric = vuln._get_metric(vuln.types[0] if getattr(vuln, "types", None) else None)
        assert type(metric).__name__ == mc.DEEPTEAM_METRIC_CLASSES[row["class"]], row["class"]
        source = inspect.getsource(type(metric))
        reads_tools = bool(re.search(r"test_case\.tools_called", source))
        assert reads_tools == (row["class"] in mc.DEEPTEAM_AGENT_FIELDS), row["class"]
