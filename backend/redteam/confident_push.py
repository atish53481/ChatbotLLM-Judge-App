"""One stored DeepTeam run sent to Confident AI as a red-teaming Risk Assessment.

The same upload red_team() makes when you are logged in (RedTeamer._post_risk_assessment):
POST /v1/risk-assessments with DeepTeam's own API schema (deepteam.red_teamer.api:
APIRiskAssessment, APIRTTestCase, APIRTTurn), so it shows under Red Teaming > Risk
Assessments on Confident AI, not as an evaluation test run. Built from the run
stored in deepteam_runs, so nothing is attacked or judged again. The key is the
app's saved Confident AI project key (side panel card), sent only to the
Confident AI region it belongs to (confident_upload._base_url).
"""
from __future__ import annotations

import requests

from backend import confident_upload
from backend.redteam.engine import _dt

ENDPOINT = "/v1/risk-assessments"


def _value(v) -> str:
    """Enum or {"value": ...} or plain: the type's string."""
    if isinstance(v, dict):
        v = v.get("value", "")
    return str(getattr(v, "value", v) or "")


def _tool_names(calls) -> list[str]:
    names = []
    for c in calls or []:
        name = c.get("name") if isinstance(c, dict) else getattr(c, "name", None)
        if name:
            names.append(str(name))
    return names


def _tool_calls(calls):
    ToolCall = _dt("deepeval.test_case").ToolCall
    out = []
    for c in calls or []:
        if isinstance(c, dict) and c.get("name"):
            out.append(ToolCall(name=str(c["name"]), input_parameters=c.get("input_parameters"),
                                output=c.get("output")))
    return out or None


def body(run: dict, identifier: str) -> dict:
    """The JSON Confident AI's risk-assessment endpoint takes, from a stored run."""
    api = _dt("deepteam.red_teamer.api")
    result = run.get("result") or {}
    overview = result.get("overview") or {}
    cases = result.get("test_cases") or []
    vuln_results = [api.VulnerabilityTypeResult(
        vulnerability=str(r.get("vulnerability") or ""), vulnerabilityType=_value(r.get("vulnerability_type")),
        passRate=float(r.get("pass_rate") or 0), passing=int(r.get("passing") or 0),
        failing=int(r.get("failing") or 0), errored=int(r.get("errored") or 0))
        for r in overview.get("vulnerability_type_results") or []]
    attack_results = [api.AttackMethodResult(
        attackMethod=str(r.get("attack_method") or ""), passRate=float(r.get("pass_rate") or 0),
        passing=int(r.get("passing") or 0), failing=int(r.get("failing") or 0), errored=int(r.get("errored") or 0))
        for r in overview.get("attack_method_results") or []]
    test_cases = []
    for i, tc in enumerate(cases):
        turns = None
        if tc.get("turns"):
            turns = [api.APIRTTurn(role=str(t.get("role") or "user"), content=str(t.get("content") or ""), order=n,
                                   retrievalContext=t.get("retrieval_context") or None,
                                   toolsCalled=_tool_calls(t.get("tools_called")),
                                   turnLevelAttack=t.get("turn_level_attack"))
                     for n, t in enumerate(tc["turns"])]
        score = tc.get("score")
        test_cases.append(api.APIRTTestCase(
            input=str(tc.get("input") or (tc["turns"][0].get("content") if tc.get("turns") else "") or ""),
            actualOutput=tc.get("actual_output"), retrievalContext=tc.get("retrieval_context") or None,
            toolsCalled=_tool_names(tc.get("tools_called")) or None, turns=turns,
            success=(float(score) > 0) if score is not None else None,
            score=float(score) if score is not None else None, reason=tc.get("reason"), error=tc.get("error"),
            vulnerability=str(tc.get("vulnerability") or ""), vulnerabilityType=_value(tc.get("vulnerability_type")),
            attackMethod=tc.get("attack_method"), riskCategory=str(tc.get("risk_category") or ""), order=i))
    duration = overview.get("run_duration")
    if duration is None:
        duration = result.get("duration_s") or 0.0
    assessment = api.APIRiskAssessment(
        vulnerabilityResults=vuln_results, attackResults=attack_results, runDuration=float(duration),
        identifier=identifier, assessmentCost=None, testCases=test_cases)
    return assessment.model_dump(by_alias=True, exclude_none=True)


def _passed(tc: dict) -> bool:
    return not tc.get("error") and tc.get("score") is not None and float(tc["score"]) >= 1


def test_run(run: dict, identifier: str, judge_model: str | None = None) -> list:
    """The run as DeepEval test runs (Confident AI > Evaluation > Test Runs), for
    plans without risk assessments: every vulnerability is a metric (score 1 =
    resisted, 0 = vulnerable, DeepTeam's reason), every attack a test case tagged
    with its vulnerability, type, attack method and risk category. Single-turn
    attacks and multi-turn ones go as two runs (Confident AI refuses a mix)."""
    from deepeval.test_run.api import ConversationalApiTestCase, LLMApiTestCase, TurnApi
    from deepeval.test_run.test_run import MetricScores, TestRun
    from deepeval.tracing.api import MetricData

    result = run.get("result") or {}
    options = run.get("options") or {}
    model = result.get("judge_model") or judge_model  # stored with the run; older runs: today's judge
    single, multi, scores = [], [], {}
    for i, tc in enumerate(result.get("test_cases") or []):
        vuln = str(tc.get("vulnerability") or "Vulnerability")
        vtype, attack = _value(tc.get("vulnerability_type")), str(tc.get("attack_method") or "Plain request")
        passed = _passed(tc)
        metric = MetricData(name=vuln, threshold=1.0, success=passed, score=tc.get("score"),
                            reason=tc.get("reason") or None, error=tc.get("error"), strictMode=True,
                            evaluationModel=model)
        bucket = scores.setdefault(vuln, {"scores": [], "passes": 0, "fails": 0, "errors": 0})
        if tc.get("error"):
            bucket["errors"] += 1
        else:
            bucket["scores"].append(float(tc.get("score") or 0))
            bucket["passes" if passed else "fails"] += 1
        tags = [t for t in (vuln, vtype, attack, str(tc.get("risk_category") or "")) if t]
        meta = {k: v for k, v in {"attack method": attack, "vulnerability type": vtype,
                                  "risk category": tc.get("risk_category"), "cvss": tc.get("cvss_score")}.items()
                if v not in (None, "")}
        name = f"{attack} → {vuln}" + (f" ({vtype})" if vtype else "")
        if tc.get("turns"):
            multi.append(ConversationalApiTestCase(
                name=name, success=passed, metricsData=[metric], scenario=str(tc.get("input") or name),
                turns=[TurnApi(role="assistant" if t.get("role") == "assistant" else "user",
                               content=str(t.get("content") or ""), order=n,
                               retrievalContext=t.get("retrieval_context") or None)
                       for n, t in enumerate(tc["turns"])],
                tags=tags, metadata=meta, order=i, runDuration=0.0))
        else:
            single.append(LLMApiTestCase(
                name=name, input=str(tc.get("input") or ""), actualOutput=tc.get("actual_output") or None,
                retrievalContext=tc.get("retrieval_context") or None, toolsCalled=_tool_calls(tc.get("tools_called")),
                success=passed, metricsData=[metric], tags=tags, metadata=meta, order=i))
    params = {"judge model": model, "framework": options.get("framework"),
              "target purpose": options.get("target_purpose"),
              "attacks per vulnerability type": options.get("attacks_per_vulnerability_type"),
              "exposure": options.get("exposure"), "source": "DeepTeam red teaming"}
    params = {k: str(v) for k, v in params.items() if v not in (None, "", [])}
    duration = float((result.get("overview") or {}).get("run_duration") or result.get("duration_s") or 0.0)
    runs = []
    for cases, convs, suffix in ((single, [], ""), ([], multi, " · multi-turn")):
        if not cases and not convs:
            continue
        mine = {m.name for c in [*cases, *convs] for m in c.metrics_data}
        runs.append(TestRun(
            testCases=cases, conversationalTestCases=convs, identifier=identifier + suffix, hyperparameters=params,
            metricsScores=[MetricScores(metric=v, **s) for v, s in scores.items() if v in mine],
            testPassed=sum(1 for c in [*cases, *convs] if c.success),
            testFailed=sum(1 for c in [*cases, *convs] if not c.success), runDuration=duration))
    return runs


def _push_as_test_run(run: dict, api_key: str, identifier: str, why: str, judge_model: str | None = None) -> dict:
    runs = test_run(run, identifier, judge_model)
    if not runs:
        raise ValueError("this DeepTeam run has no test cases to send")
    api = confident_upload.Api(api_key=api_key)
    sent = [confident_upload._send(api, r) for r in runs]
    out = {"id": sent[0]["id"], "link": sent[0]["link"], "test_cases": sum(s["test_cases"] for s in sent),
           "as": "test_run", "note": f"Sent as a test run ({why}).", "payloads": [s["payload"] for s in sent]}
    if len(sent) > 1:
        out["multi_turn_link"] = sent[1]["link"]
    return out


def _plan_refusal(message: str) -> bool:
    text = message.lower()
    return "enterprise" in text or " plan" in text or "upgrade" in text


def push(run: dict, api_key: str, identifier: str, judge_model: str | None = None) -> dict:
    """Sends the run; returns {"id", "link"}. Raises ValueError with Confident AI's message.
    When the plan has no risk assessments (Enterprise only), sends it as a test run instead."""
    if run.get("status") not in ("done", "cancelled") or not (run.get("result") or {}).get("test_cases"):
        raise ValueError("only a finished DeepTeam run with test cases can be sent")
    try:
        return _push_risk_assessment(run, api_key, identifier)
    except ValueError as e:
        if not _plan_refusal(str(e)):
            raise
        return _push_as_test_run(run, api_key, identifier,
                                 "risk assessments need Confident AI's Enterprise plan", judge_model)


def _push_risk_assessment(run: dict, api_key: str, identifier: str) -> dict:
    payload = body(run, identifier)
    version = _dt("deepteam").__version__
    headers = {"Content-Type": "application/json", "CONFIDENT_API_KEY": api_key, "X-DeepTeam-Version": version}
    response = requests.post(confident_upload._base_url(api_key) + ENDPOINT, json=payload, headers=headers,
                             timeout=120)
    try:
        data = response.json()
    except ValueError:
        data = {}
    if response.status_code != 200 or not isinstance(data, dict) or data.get("success") is False:
        detail = (data.get("error") or data.get("message") or data.get("detail")) if isinstance(data, dict) else None
        raise ValueError(f"Confident AI refused the risk assessment: {detail or f'HTTP {response.status_code}'}")
    inner = data.get("data") if isinstance(data.get("data"), dict) else {}
    return {"id": inner.get("id"), "link": data.get("link") or inner.get("link"),
            "test_cases": len(payload.get("testCases") or []), "payloads": [payload]}
