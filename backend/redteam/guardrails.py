"""DeepTeam's guardrails (deepteam.guardrails), with the judge as their model.

Seven guards: prompt injection, topical, toxicity, privacy, illegal,
hallucination and cybersecurity. check() runs the picked input guards on a
message and the picked output guards on a reply (one the caller gives, or the
chatbot's own when a client is passed), as DeepTeam's Guardrails do.
"""
from __future__ import annotations

import inspect

from backend.redteam.engine import _dt, _dump

# key: (class in deepteam.guardrails, label, guards input, guards output)
GUARDS = {
    "prompt_injection": ("PromptInjectionGuard", "Prompt injection", True, False),
    "topical": ("TopicalGuard", "Topical (stays on allowed topics)", True, False),
    "cybersecurity": ("CybersecurityGuard", "Cybersecurity", True, True),
    "toxicity": ("ToxicityGuard", "Toxicity", True, True),
    "privacy": ("PrivacyGuard", "Privacy (PII)", True, True),
    "illegal": ("IllegalGuard", "Illegal activity", True, True),
    "hallucination": ("HallucinationGuard", "Hallucination", False, True),
}


def options() -> list[dict]:
    return [{"key": k, "label": label, "input": i, "output": o} for k, (_, label, i, o) in GUARDS.items()]


def _guard(key: str, judge, allowed_topics: list[str], purpose: str, output: bool):
    if key not in GUARDS:
        raise ValueError(f"unknown guard: {key!r}; one of {', '.join(GUARDS)}")
    module = _dt("deepteam.guardrails")
    cls = getattr(module, GUARDS[key][0])
    params = inspect.signature(cls.__init__).parameters
    args = {"model": judge} if "model" in params else {}
    if key == "topical":
        topics = [t.strip() for t in allowed_topics if t.strip()]
        if not topics:
            raise ValueError("the topical guard needs at least one allowed topic")
        args["allowed_topics"] = topics
    if key == "cybersecurity":
        if purpose and "purpose" in params:
            args["purpose"] = purpose
        if "guard_type" in params:
            args["guard_type"] = module.GuardType.OUTPUT if output else module.GuardType.INPUT
    return cls(**args)


def _result(res) -> dict:
    verdicts = [_dump(v) for v in getattr(res, "verdicts", []) or []]
    return {"breached": bool(getattr(res, "breached", False)), "verdicts": verdicts}


def check(judge, text: str, input_guards: list[str], output_guards: list[str], *, output: str | None = None,
          client=None, allowed_topics: list[str] | None = None, purpose: str = "") -> dict:
    """Input guards on text; output guards on the reply (given, or the chatbot's when client is set)."""
    text = (text or "").strip()
    if not text:
        raise ValueError("type a message to guard")
    if not input_guards and not output_guards:
        raise ValueError("pick at least one guard")
    topics = list(allowed_topics or [])
    module = _dt("deepteam.guardrails")
    ins = [_guard(k, judge, topics, purpose, output=False) for k in input_guards]
    outs = [_guard(k, judge, topics, purpose, output=True) for k in output_guards]
    # Guardrails sets every guard's model from evaluation_model: the judge.
    rails = module.Guardrails(input_guards=ins, output_guards=outs, evaluation_model=judge)
    result: dict = {"input": text}
    if ins:
        result["input_result"] = _result(rails.guard_input(text))
    reply = output
    if reply is None and client is not None and outs:
        reply = client.chat(text).reply or ""
        result["reply_source"] = "chatbot"
    elif reply is not None:
        result["reply_source"] = "given"
    if outs and reply is not None:
        result["output"] = reply
        result["output_result"] = _result(rails.guard_output(text, reply))
    return result
