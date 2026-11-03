"""Profile step of the test-data build: what kind of target this is (chat, rag,
agent, mcp), which tools it has (from its config, its tool list and the calls
its replies show) and a one-sentence persona."""
from __future__ import annotations

from dataclasses import dataclass, field

from backend.cancel import RunCancelled
from backend.datasets.build_kit import BudgetSpent
from backend.datasets.autogen import PERSONA_PROMPT, TOOL_PROBE_PROMPT
from backend.judges.ask import ask_json
from backend.targets.base import ChatReply

PROBE_TOOL_MESSAGES = 3
MATERIAL_CHARS = 6000


@dataclass
class Profile:
    kind: str = ""
    tools: list[dict] = field(default_factory=list)
    persona: str = ""
    sources_seen: bool = False
    tools_seen: bool = False
    tool_reasoning_seen: bool = False


def _add_tool(profile: Profile, name, description: str = "", parameters=None) -> bool:
    """Adds a tool unless one with that name is known; True when it was new."""
    if not isinstance(name, str) or not name.strip():
        return False
    name = name.strip()
    if any(t["name"] == name for t in profile.tools):
        return False
    profile.tools.append({"name": name, "description": description or "",
                          "parameters": sorted(parameters or [])})
    return True


def _param_names(call: dict) -> list[str]:
    for key in ("input_parameters", "args", "arguments"):
        value = call.get(key)
        if isinstance(value, dict):
            return [str(k) for k in value]
    return []


def observe(profile: Profile, reply: ChatReply) -> None:
    """Learns from one reply: sources, tool calls and whether the bot explains them."""
    if reply.retrieval_context:
        profile.sources_seen = True
    for call in reply.tools_called or []:
        if not isinstance(call, dict):
            continue
        if _add_tool(profile, call.get("name"), "", _param_names(call)):
            profile.tools_seen = True
        if str(call.get("reasoning") or "").strip():
            profile.tool_reasoning_seen = True


def _schema_params(tool: dict) -> list[str]:
    schema = tool.get("input_schema") or tool.get("inputSchema") or {}
    props = schema.get("properties") if isinstance(schema, dict) else None
    if isinstance(props, dict):
        return list(props)
    params = tool.get("parameters")
    return [str(p) for p in params] if isinstance(params, (list, tuple)) else []


def _add_known(profile: Profile, tools) -> None:
    for tool in tools or []:
        if isinstance(tool, str):
            _add_tool(profile, tool)
        elif isinstance(tool, dict):
            _add_tool(profile, tool.get("name"), str(tool.get("description") or ""), _schema_params(tool))


def _probe_for_tools(client, judge, material: str, profile: Profile) -> None:
    prompt = TOOL_PROBE_PROMPT.format(n=PROBE_TOOL_MESSAGES, material=material[:MATERIAL_CHARS])
    try:
        messages = ask_json(judge, prompt).get("messages")
    except ValueError:
        return
    if not isinstance(messages, list):
        return
    sent = [m.strip() for m in messages if isinstance(m, str) and m.strip()][:PROBE_TOOL_MESSAGES]
    for message in sent:
        try:
            observe(profile, client.chat(message))
        except (BudgetSpent, RunCancelled):
            raise
        except Exception:  # noqa: BLE001 - one bad reply must not end the profile
            continue


def _persona(judge, material: str) -> str:
    try:
        text = ask_json(judge, PERSONA_PROMPT.format(material=material[:MATERIAL_CHARS])).get("persona")
    except ValueError:
        return ""
    return text.strip() if isinstance(text, str) else ""


def profile_target(client, judge, config: dict, is_mcp: bool, material: str, profile: Profile) -> Profile:
    """Fills the profile. `profile` already holds what the interview's replies showed."""
    _add_known(profile, config.get("available_tools"))
    if is_mcp:
        _add_known(profile, client.list_tools())
    # Probe only when no tool is known yet (none in the config, none seen in a reply),
    # so a rebuild after the learned tools were saved sends nothing extra.
    if config.get("tools_path") and not is_mcp and not profile.tools:
        _probe_for_tools(client, judge, material, profile)

    typed = str(config.get("persona") or "").strip()
    profile.persona = typed or _persona(judge, material)

    if is_mcp:
        profile.kind = "mcp"
    elif profile.tools:
        profile.kind = "agent"
    elif config.get("context_path") or profile.sources_seen:
        profile.kind = "rag"
    else:
        profile.kind = "chat"
    return profile
