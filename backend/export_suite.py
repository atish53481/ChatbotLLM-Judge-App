"""Exports a chatbot's evaluation as a pytest suite for the user's own repo.

The zip holds tests/evals/ (the goldens, the chosen metrics with the
environment's thresholds, a small client that calls the chatbot the way the
app does, and a pytest file) plus a GitHub Actions workflow, so the same
quality gate runs on every pull request without this app.

Header values (cookies, API keys) are never written out: the client reads them
from the LLM_EVAL_HEADERS environment variable (a JSON object), which CI sets
from a repository secret. Only Content-Type and Accept are kept.
"""
from __future__ import annotations

import io
import json
import zipfile

from backend.metrics_catalog import ask, deepeval_threshold, metric_options

# Shipped metric key -> DeepEval class for the metrics a plain export can rebuild.
STANDARD_CLASSES = {
    "answer_relevancy": "AnswerRelevancyMetric",
    "faithfulness": "FaithfulnessMetric",
    "contextual_precision": "ContextualPrecisionMetric",
    "contextual_recall": "ContextualRecallMetric",
    "contextual_relevancy": "ContextualRelevancyMetric",
    "hallucination": "HallucinationMetric",
    "bias": "BiasMetric",
    "toxicity": "ToxicityMetric",
    "pii_leakage": "PIILeakageMetric",
    "prompt_alignment": "PromptAlignmentMetric",
}
EXPORTABLE_DATASETS = ("goldens", "goldens_with_context", "safety_probes")
SAFE_HEADERS = ("content-type", "accept")


def _metric_entry(spec, threshold: float, target_config: dict) -> tuple[dict | None, str | None]:
    """The metric as metrics.json describes it, or None and why it is left out."""
    if spec.kind != "single":
        return None, "scores conversations or several replies; run it from the app"
    if spec.dataset_name not in EXPORTABLE_DATASETS:
        return None, "runs on red-team probes; run it from the app"
    if spec.key == "summarization":
        return None, "builds its own summarising prompt; run it from the app"
    entry = {"key": spec.key, "title": spec.title, "threshold": deepeval_threshold(spec, threshold),
             "needs_context": spec.dataset_name == "goldens_with_context"}
    if spec.key in STANDARD_CLASSES:
        entry.update(kind="standard", cls=STANDARD_CLASSES[spec.key])
        if spec.needs_target:
            options = metric_options(spec, None, target_config)
            if not options.get("instructions"):
                return None, "needs the chatbot's instructions, and this chatbot has none"
            entry["instructions"] = options["instructions"]
        return entry, None
    if spec.group == "custom" and spec.criteria.startswith("Decision tree: "):
        from backend.datasets import custom_metrics

        row = custom_metrics.get_metric(spec.key)
        entry.update(kind="dag", dag=row["dag"], strict=row["strict"])
        return entry, None
    if spec.criteria:
        entry.update(kind="geval", criteria=spec.criteria, params=list(spec.scores_on),
                     evaluation_steps=list(spec.evaluation_steps))
        return entry, None
    return None, "has no exportable definition"


def _golden_rows(goldens: list[dict]) -> list[dict]:
    return [{"id": g.get("id"), "question": g["question"], "message": ask(g),
             "expected_answer": g.get("expected_answer", ""), "context": g.get("context") or []}
            for g in goldens if g.get("status") != "draft"]


def _client_config(config: dict) -> dict:
    headers = config.get("headers") or {}
    return {
        "url": config.get("url", ""),
        "method": config.get("method", "POST"),
        "body_template": config.get("body_template", ""),
        "response_path": config.get("response_path", ""),
        "headers": {k: v for k, v in headers.items() if k.lower() in SAFE_HEADERS},
        "secret_header_names": sorted(k for k in headers if k.lower() not in SAFE_HEADERS),
    }


CLIENT_PY = '''"""Calls the chatbot under test the way the LLM Judge app does (generated)."""
import json
import os
from pathlib import Path
from urllib.parse import quote, quote_plus

import requests

CONFIG = json.loads((Path(__file__).parent / "chatbot.json").read_text(encoding="utf-8"))
PLACEHOLDER = "{{message}}"


def headers() -> dict:
    """Saved non-secret headers plus the secret ones from LLM_EVAL_HEADERS (JSON)."""
    secret = json.loads(os.environ.get("LLM_EVAL_HEADERS") or "{}")
    return {**CONFIG["headers"], **secret}


def _encode(message: str, all_headers: dict) -> str:
    content_type = next((v for k, v in all_headers.items() if k.lower() == "content-type"), "").lower()
    if "x-www-form-urlencoded" in content_type:
        return quote_plus(message)
    return json.dumps(message, ensure_ascii=False)[1:-1]


def _get_path(obj, dotted: str):
    for part in dotted.split("."):
        obj = obj[int(part)] if isinstance(obj, list) else obj[part]
    return obj


def ask(message: str) -> str:
    all_headers = headers()
    url = CONFIG["url"].replace(PLACEHOLDER, quote(message, safe=""))
    body = None
    if CONFIG["body_template"]:
        body = CONFIG["body_template"].replace(PLACEHOLDER, _encode(message, all_headers)).encode("utf-8")
    r = requests.request(CONFIG["method"], url, data=body, headers=all_headers, timeout=60)
    r.raise_for_status()
    path = CONFIG["response_path"]
    text = r.text
    if text.lstrip().startswith("data:"):
        pieces = []
        for line in text.splitlines():
            payload = line[5:].strip() if line.startswith("data:") else ""
            if not payload or payload == "[DONE]":
                continue
            try:
                event = json.loads(payload)
                pieces.append(_get_path(event, path) if path else payload)
            except (ValueError, KeyError, IndexError, TypeError):
                continue
        return "".join(p for p in pieces if isinstance(p, str))
    if not path:
        return text
    return str(_get_path(r.json(), path))
'''

TEST_PY = '''"""LLM quality gate, generated by the LLM Judge app. Run: pytest tests/evals

Needs JUDGE_API_KEY (and optionally JUDGE_MODEL, JUDGE_BASE_URL) for the judge,
and LLM_EVAL_HEADERS for the chatbot's secret headers. Each case calls the
chatbot once and the judge a few times.
"""
import json
import os
from pathlib import Path

import pytest
from deepeval import assert_test
from deepeval import metrics as dm
from deepeval.metrics.dag import dag_from_dict
from deepeval.models import LocalModel
from deepeval.test_case import LLMTestCase, SingleTurnParams

from chatbot import ask

HERE = Path(__file__).parent
GOLDENS = json.loads((HERE / "goldens.json").read_text(encoding="utf-8"))
METRICS = json.loads((HERE / "metrics.json").read_text(encoding="utf-8"))


def judge():
    return LocalModel(
        model=os.environ.get("JUDGE_MODEL") or "openai/gpt-oss-120b",
        base_url=os.environ.get("JUDGE_BASE_URL") or "https://api.groq.com/openai/v1",
        api_key=os.environ["JUDGE_API_KEY"], temperature=0.0, format="json",
    )


def build_metric(entry):
    common = {"threshold": entry["threshold"], "model": judge(), "async_mode": False}
    if entry["kind"] == "geval":
        params = [SingleTurnParams(p) for p in entry["params"]]
        return dm.GEval(name=entry["title"], criteria=entry["criteria"],
                        evaluation_steps=entry.get("evaluation_steps") or None, evaluation_params=params, **common)
    if entry["kind"] == "dag":
        return dm.DAGMetric(name=entry["title"], dag=dag_from_dict(entry["dag"]), strict_mode=entry["strict"],
                            **common)
    cls = getattr(dm, entry["cls"])
    if entry.get("instructions"):
        return cls(prompt_instructions=entry["instructions"], include_reason=True, **common)
    return cls(include_reason=True, **common)


CASES = [(m, g) for m in METRICS for g in GOLDENS if g["context"] or not m["needs_context"]]


@pytest.mark.parametrize("entry, golden", CASES, ids=[f"{m['key']}-{g['id']}" for m, g in CASES])
def test_chatbot(entry, golden):
    reply = ask(golden["message"])
    case = LLMTestCase(
        input=golden["question"], actual_output=reply,
        expected_output=golden["expected_answer"] or None,
        context=golden["context"] or None, retrieval_context=golden["context"] or None,
    )
    assert_test(case, [build_metric(entry)], run_async=False)
'''

WORKFLOW_YML = '''name: LLM evals
on:
  pull_request:
  workflow_dispatch:
jobs:
  evals:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.13"
      - run: pip install "deepeval>=4.3.1,<5" requests pytest
      - run: pytest tests/evals -q
        env:
          PYTHONPATH: tests/evals
          JUDGE_API_KEY: ${{ secrets.JUDGE_API_KEY }}
          JUDGE_MODEL: ${{ vars.JUDGE_MODEL }}
          JUDGE_BASE_URL: ${{ vars.JUDGE_BASE_URL }}
          LLM_EVAL_HEADERS: ${{ secrets.LLM_EVAL_HEADERS }}
'''


def _readme(target_name: str, environment: str, metrics: list[dict], skipped: list[dict],
            secret_names: list[str], n_goldens: int) -> str:
    lines = [
        f"# LLM evals for {target_name}",
        "",
        f"Generated by the LLM Judge app: {len(metrics)} metric(s) x {n_goldens} golden(s), "
        f"thresholds for the {environment!r} environment.",
        "",
        "## Run locally",
        "",
        "    pip install \"deepeval>=4.3.1,<5\" requests pytest",
        "    set JUDGE_API_KEY=...            (macOS/Linux: export)",
        "    set LLM_EVAL_HEADERS={\"Header\": \"value\"}",
        "    set PYTHONPATH=tests/evals",
        "    pytest tests/evals -q",
        "",
        "## CI",
        "",
        "`.github/workflows/llm-evals.yml` runs the suite on every pull request. Add the repository",
        "secrets `JUDGE_API_KEY` and `LLM_EVAL_HEADERS`; `JUDGE_MODEL` / `JUDGE_BASE_URL` are optional",
        "variables.",
        "",
        "## Secret headers",
        "",
        "Header values were not exported. Put these in LLM_EVAL_HEADERS as a JSON object:",
        "",
        *([f"- `{name}`" for name in secret_names] or ["- (none: this chatbot needs no secret headers)"]),
        "",
        "Check `chatbot.json` before committing: a token inside the URL or the body template is",
        "exported as captured.",
        "",
        "## Metrics",
        "",
        *[f"- {m['title']} (DeepEval threshold {m['threshold']})" for m in metrics],
    ]
    if skipped:
        lines += ["", "## Not exported", "", *[f"- {s['title']}: {s['why']}" for s in skipped]]
    return "\n".join(lines) + "\n"


def build_suite(target: dict, specs: list, thresholds: dict, environment: str,
                goldens: list[dict]) -> dict[str, str]:
    """{path in the zip: file content}. thresholds: {key: user-facing threshold}."""
    config = target["config"]
    metrics, skipped = [], []
    for spec in specs:
        entry, why = _metric_entry(spec, thresholds.get(spec.key, spec.threshold), config)
        if entry is None:
            skipped.append({"key": spec.key, "title": spec.title, "why": why})
        else:
            metrics.append(entry)
    client = _client_config(config)
    rows = _golden_rows(goldens)
    return {
        "tests/evals/goldens.json": json.dumps(rows, indent=2, ensure_ascii=False),
        "tests/evals/metrics.json": json.dumps(metrics, indent=2, ensure_ascii=False),
        "tests/evals/chatbot.json": json.dumps(
            {k: v for k, v in client.items() if k != "secret_header_names"}, indent=2, ensure_ascii=False),
        "tests/evals/chatbot.py": CLIENT_PY,
        "tests/evals/test_chatbot_evals.py": TEST_PY,
        "tests/evals/README.md": _readme(target["name"], environment, metrics, skipped,
                                         client["secret_header_names"], len(rows)),
        ".github/workflows/llm-evals.yml": WORKFLOW_YML,
    }


def zip_suite(files: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for path, content in files.items():
            zf.writestr(path, content)
    return buffer.getvalue()
