"""Send a finished scan to Confident AI (app.confident-ai.com) as a DeepEval test run.

The run is built from what the scan already stored (every case's input,
reply, context, tool calls and each metric's score, verdict and reason), so
nothing is judged again and no judge tokens are spent. It goes out the way
`deepeval test run` sends one: POST /v1/test-run with the first batch of test
cases, then PUT the rest. DeepEval's own uploader is not called because it
also opens a browser and writes local files.

The API key comes only from the CONFIDENT_API_KEY environment variable (.env)
and is never returned, logged or sent anywhere else.
"""
from __future__ import annotations

import json
import os
from datetime import datetime

import requests
from deepeval.confident.api import Api, Endpoints, HttpMethods, _api_url_for_region, _infer_region_from_api_key
from deepeval.test_case import ToolCall
from deepeval.test_run.api import LLMApiTestCase
from deepeval.test_run.test_run import MetricScores, RemainingTestRun, TestRun, TestRunEncoder
from deepeval.tracing.api import MetricData

KEY_ENV = "CONFIDENT_API_KEY"
BATCH_SIZE = 40  # test cases per request, as DeepEval sends them


def api_key() -> str | None:
    return (os.getenv(KEY_ENV) or "").strip() or None


# --- Organization keys -------------------------------------------------------
# Confident AI keys carry their scope in the prefix: confident_<region>_org_...
# (organization admin) or confident_<region>_proj_... (one project: evals and
# traces). Test runs need a project key, so an org key plus a project name is
# turned into one through the v2 API: GET /v2/projects, then
# POST /v2/projects/{id}/api-keys (the new key's value is returned once).
_http = requests  # tests swap in a fake
APP_KEY_NAME = "LLM Judge app"


class ConfidentKeyError(ValueError):
    """The key or project cannot be used; the message says what to do."""


def is_org_key(key: str) -> bool:
    return "_org_" in key.split("=")[0][:24]


def _base_url(key: str) -> str:
    return _api_url_for_region(_infer_region_from_api_key(key))


def _data(response, what: str) -> dict:
    try:
        body = response.json()
    except ValueError:
        body = {}
    if response.status_code != 200 or not isinstance(body, dict) or not body.get("success", True):
        detail = (body.get("error") or body.get("message") or body.get("detail")) if isinstance(body, dict) else None
        raise ConfidentKeyError(f"Confident AI refused to {what}: {detail or f'HTTP {response.status_code}'}")
    return body.get("data") or {}


def project_key_from_org(org_key: str, project: str) -> tuple[str, str]:
    """(new project key, the project's exact name) for the named project."""
    if not project.strip():
        raise ConfidentKeyError("This is an organization key (…_org_…). Enter the project name too, or paste "
                                "the project's own key (…_proj_…) from the project's Settings → API Keys.")
    base, headers = _base_url(org_key), {"CONFIDENT_API_KEY": org_key, "Content-Type": "application/json"}
    projects = _data(_http.get(f"{base}/v2/projects", headers=headers, timeout=30), "list your projects").get("projects") or []
    match = next((p for p in projects if (p.get("name") or "").strip().lower() == project.strip().lower()), None)
    if match is None:
        names = ", ".join(repr(p.get("name")) for p in projects) or "none"
        raise ConfidentKeyError(f"No project named {project.strip()!r} in this organization (projects: {names}).")
    made = _data(_http.post(f"{base}/v2/projects/{match['id']}/api-keys", headers=headers,
                            json={"name": APP_KEY_NAME}, timeout=30), "create a project key")
    if not made.get("value"):
        raise ConfidentKeyError("Confident AI did not return the new project key.")
    return made["value"], match.get("name") or project.strip()


def _seconds(start: str | None, end: str | None) -> float:
    try:
        parse = lambda ts: datetime.fromisoformat(ts.replace("Z", "+00:00"))  # noqa: E731
        return max(0.0, (parse(end) - parse(start)).total_seconds())
    except (AttributeError, TypeError, ValueError):
        return 0.0


def _judge_model(result: dict) -> str | None:
    model = (result.get("judge") or {}).get("model")
    return model.replace(" (Local Model)", "") if model else None


def _tool(t: dict) -> ToolCall:
    return ToolCall(name=t.get("name") or "tool", input_parameters=t.get("input_parameters") or None,
                    output=t.get("output"), reasoning=t.get("reasoning"))


def _json(model) -> dict:
    """A DeepEval model as the JSON body Confident AI expects (camelCase aliases)."""
    return json.loads(json.dumps(model.model_dump(by_alias=True, exclude_none=True), cls=TestRunEncoder))


def _test_cases(results: list[dict], titles: dict) -> list[LLMApiTestCase]:
    """One test case per golden (or probe), with every metric's verdict on it,
    in the order the cases first appear (as the runs page shows them)."""
    cases: dict[str, dict] = {}
    for result in results:
        name = titles.get(result["key"], result["key"])
        for row in result.get("rows") or []:
            key = f"id:{row['golden_id']}" if row.get("golden_id") else f"q:{row.get('input') or row.get('question') or ''}"
            case = cases.setdefault(key, {"row": row, "metrics": []})
            if not case["row"].get("tools_called") and row.get("tools_called"):
                case["row"] = {**case["row"], "tools_called": row["tools_called"]}
            case["metrics"].append(MetricData(
                name=name, threshold=result.get("threshold"), success=bool(row.get("passed")),
                score=row.get("score"), reason=row.get("reason") or None, error=row.get("error"),
                evaluationModel=_judge_model(result), strictMode=False))
    out = []
    for i, case in enumerate(cases.values()):
        row = case["row"]
        out.append(LLMApiTestCase(
            name=f"test_case_{i}", input=row.get("input") or row.get("question") or "",
            actualOutput=row.get("actual_output") or None, expectedOutput=row.get("expected_output") or None,
            retrievalContext=[str(c) for c in row.get("context") or []] or None,
            toolsCalled=[_tool(t) for t in row.get("tools_called") or []] or None,
            success=all(m.success for m in case["metrics"]), metricsData=case["metrics"],
            runDuration=row.get("latency_s"), order=i))
    return out


def _metric_scores(results: list[dict], titles: dict) -> list[MetricScores]:
    out = []
    for result in results:
        rows = result.get("rows") or []
        scores = [r["score"] for r in rows if isinstance(r.get("score"), (int, float))]
        passes = sum(1 for r in rows if r.get("passed"))
        out.append(MetricScores(metric=titles.get(result["key"], result["key"]), scores=scores, passes=passes,
                                fails=len(rows) - passes, errors=1 if result.get("status") == "error" else 0))
    return out


def _hyperparameters(scan: dict, chatbot: str, theme: str) -> dict:
    options = scan.get("run_options") or {}
    deepeval = options.get("deepeval") or {}
    judge = next((m for m in map(_judge_model, scan.get("results") or []) if m), None)
    params = {
        "judge model": judge, "chatbot": chatbot, "golden set": theme,
        "cases per run": options.get("limit") or "all",
        "parallel metrics": options.get("metric_concurrency"),
        "strict mode": deepeval.get("strict_mode"),
        "consistency check": options.get("check_consistency"),
        "attack enhancements": ", ".join(options.get("enhancements") or []) or None,
    }
    return {k: str(v) for k, v in params.items() if v is not None}


def build_test_run(scan: dict, chatbot: str, theme: str, titles: dict) -> TestRun:
    results = scan.get("results") or []
    cases = _test_cases(results, titles)
    cost = sum(r["judge_cost_usd"] for r in results if isinstance(r.get("judge_cost_usd"), (int, float)))
    return TestRun(
        testCases=cases, metricsScores=_metric_scores(results, titles),
        identifier=f"{chatbot} · v{scan.get('version')}", hyperparameters=_hyperparameters(scan, chatbot, theme),
        testPassed=sum(1 for c in cases if c.success), testFailed=sum(1 for c in cases if not c.success),
        runDuration=_seconds(scan.get("started_at"), scan.get("finished_at")),
        evaluationCost=cost or None, datasetAlias=theme)


def test_run_body(scan: dict, chatbot: str, theme: str, titles: dict) -> dict:
    """The JSON Confident AI receives for this scan (first batch and all)."""
    return _json(build_test_run(scan, chatbot, theme, titles))


def upload(scan: dict, chatbot: str, theme: str, titles: dict, api_key: str) -> dict:
    """Sends the scan; returns {"id", "link", "test_cases"}. Raises ValueError
    when there is nothing to send, and the API's own error otherwise."""
    run = build_test_run(scan, chatbot, theme, titles)
    cases = run.test_cases
    if not cases:
        raise ValueError("this test run has no test cases to send")
    api = Api(api_key=api_key)
    run.test_cases = cases[:BATCH_SIZE]
    data, link = api.send_request(method=HttpMethods.POST, endpoint=Endpoints.TEST_RUN_ENDPOINT, body=_json(run))
    if not isinstance(data, dict) or "id" not in data:
        raise RuntimeError("Confident AI did not accept the test run")
    for start in range(BATCH_SIZE, len(cases), BATCH_SIZE):
        rest = RemainingTestRun(testRunId=data["id"], testCases=cases[start:start + BATCH_SIZE], conversationalTestCases=[])
        api.send_request(method=HttpMethods.PUT, endpoint=Endpoints.TEST_RUN_ENDPOINT, body=_json(rest))
    return {"id": data["id"], "link": link, "test_cases": len(cases)}
