"""Exports a chatbot's evaluation as a pytest suite for the user's own repo.

The zip holds tests/evals/ (the goldens, the chosen metrics with the
environment's thresholds, a small client that calls the chatbot the way the
app does, and a pytest file) plus a GitHub Actions workflow, so the same
quality gate runs on every pull request without this app.

Red-team metrics (the security probes, DeepTeam's included, and No-Prompt-Leak)
send their own probes instead of the goldens: probes.json holds the ones a run
of each would send this chatbot (its probe set; for security probes its role
as each probe's context, as MetricSpec.cases builds them), and the pytest file
scores every reply with the metric's G-Eval, as the app does. Bias and Toxicity
send their attack prompts first and the goldens after ("cases": "probes+goldens"),
exactly as a run does. The adaptive multi-turn attack and the attack enhancers
stay in the app.

Header values (cookies, API keys) are never written out: the client reads them
from the LLM_EVAL_HEADERS environment variable (a JSON object), which CI sets
from a repository secret. Only Content-Type and Accept are kept.
"""
from __future__ import annotations

import io
import json
import zipfile

from backend.datasets.security_probes import DEFAULT_PROBE_SET, load_probes
from backend.metrics_catalog import DEFAULT_THEME, ask, deepeval_threshold, metric_options

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
# Red-team metrics: each sends its own probes (backend/datasets/security_probes.py,
# the chatbot's probe set) instead of the goldens; build_suite writes them to probes.json.
RED_TEAM_DATASETS = ("security_probes", "prompt_leak_probes")
# Metrics that send their attack prompts first and the goldens after (a run sends both).
MIXED_DATASETS = ("safety_probes",)
SAFE_HEADERS = ("content-type", "accept")

ATTACK_WHY = ("an adaptive multi-turn attack (the judge plays the attacker and writes each next message from "
              "the chatbot's last reply), so there is no fixed test case to export; run it from the app")
ENHANCERS_WHY = ("not applied; probes.json holds every probe as written. The app builds the disguised variants "
                 "(Base64, ROT13, role-play, many-shot, multilingual and DeepTeam's single-turn attacks) on "
                 "every run, some written by the judge, and never stores them; run them from the app "
                 "(Advanced tools, Run options)")
# Why a single-turn metric whose test cases the export does not carry is left out.
DATASET_WHY = {
    "goldens_with_tools": ("scores the tools the chatbot calls against the goldens' expected tools, which "
                           "the exported client does not read; run it from the app"),
}
OTHER_DATASET_WHY = "runs on test cases the export does not include; run it from the app"


def _metric_entry(spec, threshold: float, target_config: dict) -> tuple[dict | None, str | None]:
    """The metric as metrics.json describes it, or None and why it is left out.
    A red-team metric's entry says "cases": "probes" (build_suite adds its probes);
    a safety metric says "cases": "probes+goldens" (its attack prompts first, the
    goldens after, as a run sends both)."""
    if spec.kind == "attack":
        return None, ATTACK_WHY
    if spec.kind != "single":
        return None, "scores conversations or several replies; run it from the app"
    red_team = spec.dataset_name in RED_TEAM_DATASETS
    if spec.dataset_name not in EXPORTABLE_DATASETS and not red_team:
        return None, DATASET_WHY.get(spec.dataset_name, OTHER_DATASET_WHY)
    if spec.key == "summarization":
        return None, "builds its own summarising prompt; run it from the app"
    entry = {"key": spec.key, "title": spec.title, "threshold": deepeval_threshold(spec, threshold),
             "needs_context": spec.dataset_name == "goldens_with_context"}
    if red_team:
        entry["cases"] = "probes"
    # Bias / Toxicity get "cases": "probes+goldens" in _add_probes, only when the
    # chatbot's probe set has probes for them; with none they export as before.
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


def _probe_rows(spec, config: dict, probe_set: str) -> list[dict]:
    """The probes a run of this metric sends this chatbot, built by
    MetricSpec.cases as the runner builds them (the chatbot's probe set; for
    security probes its role, or the no-role note, as each probe's context),
    in goldens.json's row shape. Bias / Toxicity send their probes first, then
    the goldens (already in goldens.json): only the probes are kept. Raises
    when the probe set cannot be read."""
    items = spec.cases(theme=config.get("theme") or DEFAULT_THEME, persona=config.get("persona") or "",
                       probe_set=probe_set)
    if spec.dataset_name in MIXED_DATASETS:
        items = items[:len(load_probes(metric=spec.key, probe_set=probe_set))]
    return [{"id": item.get("id"), "question": item["question"], "message": spec.prompt(item),
             "expected_answer": item.get("expected_answer") or "", "context": list(item.get("context") or [])}
            for item in items]


def _add_probes(spec, entry: dict, config: dict, probes: dict) -> tuple[dict | None, str | None]:
    """Puts the probes a run of this metric sends in `probes`. A red-team
    metric sends only them ("cases": "probes"); Bias / Toxicity send theirs
    before the goldens ("cases": "probes+goldens"), or the goldens alone, as
    before, when the probe set has none. None and why when nothing can be sent."""
    probe_set = config.get("probe_set") or DEFAULT_PROBE_SET
    try:
        rows = _probe_rows(spec, config, probe_set)
    except Exception as e:  # noqa: BLE001 - an unreadable probe set leaves this metric out, not the export
        return None, f"its probes could not be read ({type(e).__name__}: {e}); run it from the app"
    red_team = spec.dataset_name in RED_TEAM_DATASETS
    if red_team and not rows:
        return None, f"has no probes in this chatbot's {probe_set!r} probe set; add some in the app"
    if rows:
        probes[spec.key] = rows
        if spec.dataset_name in MIXED_DATASETS:
            entry["cases"] = "probes+goldens"
    return entry, None


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
chatbot once and the judge a few times. Red-team metrics ("cases": "probes")
send their own attack messages from probes.json instead of the goldens; Bias
and Toxicity ("cases": "probes+goldens") send theirs first, then the goldens.
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
# {metric key: [probe]} for the metrics that send probes; no file when none does.
PROBES_FILE = HERE / "probes.json"
PROBES = json.loads(PROBES_FILE.read_text(encoding="utf-8")) if PROBES_FILE.exists() else {}


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


def golden_cases(m):
    """What a golden metric sends: Bias / Toxicity ("cases": "probes+goldens")
    send their adversarial probes first, then the goldens, as the app does."""
    probes = PROBES.get(m["key"], []) if m.get("cases") == "probes+goldens" else []
    return probes + [g for g in GOLDENS if g["context"] or not m["needs_context"]]


CASES = [(m, g) for m in METRICS if m.get("cases") != "probes" for g in golden_cases(m)]
PROBE_CASES = [(m, p) for m in METRICS if m.get("cases") == "probes" for p in PROBES.get(m["key"], [])]


@pytest.mark.parametrize("entry, golden", CASES, ids=[f"{m['key']}-{g['id']}" for m, g in CASES])
def test_chatbot(entry, golden):
    reply = ask(golden["message"])
    case = LLMTestCase(
        input=golden["question"], actual_output=reply,
        expected_output=golden["expected_answer"] or None,
        context=golden["context"] or None, retrieval_context=golden["context"] or None,
    )
    assert_test(case, [build_metric(entry)], run_async=False)


if PROBE_CASES:  # only when red-team metrics were exported
    @pytest.mark.parametrize("entry, probe", PROBE_CASES, ids=[f"{m['key']}-{p['id']}" for m, p in PROBE_CASES])
    def test_red_team(entry, probe):
        """One attack: the judge scores whether the reply resisted it, against the
        chatbot's intended role (the probe's context) when the metric reads it."""
        reply = ask(probe["message"])
        case = LLMTestCase(input=probe["question"], actual_output=reply, context=probe["context"] or None)
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


def _metric_line(entry: dict, probes: dict) -> str:
    line = f"- {entry['title']} (DeepEval threshold {entry['threshold']})"
    if entry.get("cases") == "probes":
        line += f" on {len(probes.get(entry['key'], []))} probe(s)"
    elif entry.get("cases") == "probes+goldens":
        line += f" on {len(probes.get(entry['key'], []))} probe(s), then the goldens"
    return line


def _readme(target_name: str, environment: str, metrics: list[dict], skipped: list[dict],
            secret_names: list[str], n_goldens: int, probes: dict | None = None, probe_set: str = "") -> str:
    probes = probes or {}
    red_team = [m for m in metrics if m.get("cases") == "probes"]
    mixed = [m for m in metrics if m.get("cases") == "probes+goldens"]
    summary = f"{len(metrics) - len(red_team)} metric(s) x {n_goldens} golden(s)"
    if red_team:
        summary += (f" and {len(red_team)} red-team metric(s) x their own probes "
                    f"({sum(len(probes.get(m['key'], [])) for m in red_team)} in all)")
    lines = [
        f"# LLM evals for {target_name}",
        "",
        f"Generated by the LLM Judge app: {summary}, "
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
    ]
    if red_team:
        lines += [
            "## Red-team probes",
            "",
            'Red-team metrics (`"cases": "probes"` in `metrics.json`) send the attack messages in',
            f"`probes.json` instead of the goldens: this chatbot's {probe_set!r} probe set as the app sends",
            "it, one `test_red_team` case per probe. The judge scores each reply with the metric's G-Eval",
            "criteria and fixed evaluation steps; a security probe's context is the chatbot's intended",
            "role (its Chatbot role in the app). Change the probes in the app and export again.",
            "",
        ]
    if mixed:
        names = " and ".join(m["title"] for m in mixed)
        lines += [
            "## Adversarial probes",
            "",
            f'{names} (`"cases": "probes+goldens"` in `metrics.json`) send their adversarial probes in',
            f"`probes.json` (this chatbot's {probe_set!r} probe set) first, then the goldens, as the app does,",
            "each case scored with the same metric. Change the probes in the app and export again.",
            "",
        ]
    lines += ["## Metrics", "", *[_metric_line(m, probes) for m in metrics]]
    if skipped:
        lines += ["", "## Not exported", "", *[f"- {s['title']}: {s['why']}" for s in skipped]]
    return "\n".join(lines) + "\n"


def build_suite(target: dict, specs: list, thresholds: dict, environment: str,
                goldens: list[dict]) -> dict[str, str]:
    """{path in the zip: file content}. thresholds: {key: user-facing threshold}."""
    config = target["config"]
    metrics, skipped, probes = [], [], {}
    for spec in specs:
        entry, why = _metric_entry(spec, thresholds.get(spec.key, spec.threshold), config)
        if entry is not None and spec.dataset_name in RED_TEAM_DATASETS + MIXED_DATASETS:
            entry, why = _add_probes(spec, entry, config, probes)
        if entry is None:
            skipped.append({"key": spec.key, "title": spec.title, "why": why})
        else:
            metrics.append(entry)
    if probes:
        skipped.append({"key": "attack_enhancers", "title": "Attack enhancers", "why": ENHANCERS_WHY})
    client = _client_config(config)
    rows = _golden_rows(goldens)
    files = {
        "tests/evals/goldens.json": json.dumps(rows, indent=2, ensure_ascii=False),
        "tests/evals/metrics.json": json.dumps(metrics, indent=2, ensure_ascii=False),
    }
    if probes:  # only when a metric sends probes, so other exports keep the same files
        files["tests/evals/probes.json"] = json.dumps(probes, indent=2, ensure_ascii=False)
    files.update({
        "tests/evals/chatbot.json": json.dumps(
            {k: v for k, v in client.items() if k != "secret_header_names"}, indent=2, ensure_ascii=False),
        "tests/evals/chatbot.py": CLIENT_PY,
        "tests/evals/test_chatbot_evals.py": TEST_PY,
        "tests/evals/README.md": _readme(target["name"], environment, metrics, skipped,
                                         client["secret_header_names"], len(rows), probes,
                                         config.get("probe_set") or DEFAULT_PROBE_SET),
        ".github/workflows/llm-evals.yml": WORKFLOW_YML,
    })
    return files


def zip_suite(files: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for path, content in files.items():
            zf.writestr(path, content)
    return buffer.getvalue()
