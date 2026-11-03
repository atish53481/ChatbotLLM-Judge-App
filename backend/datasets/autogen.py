"""A dataset for whichever chatbot is connected, not only the sample shop.

When a chatbot (or agent, or RAG bot) is added, the app can learn it three ways:

1. interview: ask the chatbot what it helps with, what it covers and what it
   will not do (a few ordinary messages, like a curious user);
2. find_help_pages: look for its FAQ / help / docs pages on its own website
   (from the chatbot's URL, the page that hosts it, or a help URL given);
3. write_dataset: the judge writes golden answers (question + the behaviour a
   good answer shows) and multi-turn conversation scenarios fitted to what the
   interview and the pages say.

The pages found also go through the existing document pipeline, which makes
golden answers with Context (the facts) for the RAG metrics.
"""
from __future__ import annotations

from typing import Callable, Sequence
from urllib.parse import urlsplit

from backend.cancel import RunCancelled
from backend.datasets.build_kit import BudgetSpent, is_duplicate
from backend.judges.ask import ask_json
from backend.rag.fetch import fetch_page_text
from backend.targets.base import ChatReply

# Labels the side panel shows for what it can generate from.
SOURCE_CHATBOT = "the chatbot itself"
SOURCE_SEARCH = "its FAQ / help pages (searched on its website)"
GOLDEN_SOURCE = "auto_chatbot"

INTERVIEW = [
    "Hi! What can you help me with?",
    "Which products, services or topics can you answer questions about?",
    "Is there anything you can't help with, or that I should ask someone else about?",
]

# Where sites usually keep their FAQ, help centre or docs.
HELP_PATHS = ["/faq", "/faqs", "/help", "/support", "/help-center", "/docs",
              "/documentation", "/knowledge-base", "/kb", "/customer-service"]
MAX_PAGES = 3          # pages kept
MAX_ATTEMPTS = 12      # fetches tried, so a slow or empty site cannot stall the panel
PROMPT_TEXT_CHARS = 6000

GOLDENS_WANTED = 8
SCENARIOS_WANTED = 3

# Sub-domains that serve the chatbot's API rather than the website.
_API_PREFIXES = ("api.", "chat.", "bot.", "app.", "backend.", "gateway.")
_SITE_HEADERS = ("origin", "referer")


def _site(url: str) -> str | None:
    parts = urlsplit((url or "").strip())
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    return f"{parts.scheme}://{parts.netloc}"


def _is_local(host: str) -> bool:
    name = host.split(":", 1)[0]
    return name == "localhost" or name.replace(".", "").isdigit() or "." not in name


def candidate_sites(config: dict) -> list[str]:
    """Websites to look on, best first: a help URL already given, the page the
    chatbot runs on (Origin / Referer of the captured request), then the
    chatbot's own host and, for an api. / chat. host, its main domain."""
    sites: list[str] = []

    def add(site):
        if site and site not in sites:
            sites.append(site)

    add(_site(config.get("help_url", "")))
    headers = {k.lower(): v for k, v in (config.get("headers") or {}).items() if isinstance(v, str)}
    for name in _SITE_HEADERS:
        if headers.get(name) and headers[name] != "***":  # masked values are not addresses
            add(_site(headers[name]))
    for site in list(sites) + [_site(config.get("url", ""))]:
        if not site:
            continue
        scheme, host = site.split("://", 1)
        if _is_local(host):
            add(site)  # localhost / an IP address: only that host
            continue
        bare = host[4:] if host.startswith("www.") else host
        bare = next((bare[len(p):] for p in _API_PREFIXES if bare.startswith(p)), bare)
        if bare == host:
            add(site)
        else:
            add(f"{scheme}://www.{bare}")
            add(f"{scheme}://{bare}")
    return sites


def find_help_pages(config: dict, fetch=fetch_page_text) -> list[tuple[str, str]]:
    """(url, text) of up to MAX_PAGES FAQ / help / docs pages that answer."""
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    given = (config.get("help_url") or "").strip()
    urls = ([given] if given else []) + [site + path for site in candidate_sites(config) for path in HELP_PATHS]
    for attempt, url in enumerate(urls):
        if len(found) >= MAX_PAGES or attempt >= MAX_ATTEMPTS:
            break
        try:
            text, final_url = fetch(url)
        except ValueError:
            continue
        if final_url in seen:
            continue  # several paths redirecting to one page
        seen.add(final_url)
        found.append((final_url, text))
    return found


def interview(target, on_reply: Callable[[ChatReply], None] | None = None) -> list[tuple[str, str]]:
    """The chatbot's own answers about what it does; [] when it cannot answer.
    on_reply, when given, sees every reply (the profile step learns tools from them)."""
    answers = []
    for question in INTERVIEW:
        try:
            full = target.chat(question)
        except RunCancelled:
            raise  # Stop ends the build
        except Exception:  # noqa: BLE001 - an unreachable bot just means no interview
            break
        if on_reply is not None:
            on_reply(full)
        reply = full.reply
        if reply and reply.strip():
            answers.append((question, reply.strip()))
    return answers


PERSONA_PROMPT = """From the material below, describe in ONE sentence who this chatbot is and who it serves
(for example "Customer support assistant for the Acme online shop.").
Use only what the material supports; do not invent facts.

What the chatbot said when asked about itself and its help pages:
{material}

Return only JSON: {{"persona": "<one sentence>"}}"""

TOOL_PROBE_PROMPT = """A chatbot may be able to call tools (look up an order, check a price, book something).
From the material below, write {n} short user messages that ask it to DO something concrete, the kind of
request that would make such a chatbot call a tool. Use realistic ids or names only when the material gives them.

Material about the chatbot:
{material}

Return only JSON: {{"messages": ["", ""]}}"""


WRITE_PROMPT = """You are building a test dataset for a chatbot so that it can be evaluated.
Learn what this chatbot is from the material below, then write test data that fits it.
{role}
What the chatbot said when asked about itself:
{interview}

Its help / FAQ / documentation pages:
{pages}

Write:
1. "goldens": {n_goldens} realistic questions real users would ask THIS chatbot, a mix of these
   categories (put one in "category"): "happy_path" (ordinary questions on its main topics),
   "edge_case", "out_of_scope" (things it should decline or redirect), "multi_intent" (two questions in
   one message) and "follow_up" (a short question that only makes sense after an earlier one).
   Every question must be different from the others. For each, "expected_answer" describes what a
   good reply does (facts only when the material states them; otherwise the behaviour, e.g.
   "Explains how to start a return and mentions the 30-day window").{short}
2. "scenarios": {n_scenarios} multi-turn conversations a real user would have with it. Each has
   "name", "scenario" (one sentence), "chatbot_role" (who the bot is, never empty), "expected_outcome"
   (what the user should end up with, never empty) and "user_turns" (2 to 4 user messages; one early
   message states a fact, and a later turn depends on that fact).
{avoid}
Expected answers come from the material above or from the scope the chatbot says it has. Never quote
the chatbot's own replies as the expected answer, and never invent policies, prices or facts the
material does not support. Return only JSON:
{{"goldens": [{{"question": "", "expected_answer": "", "category": ""}}],
  "scenarios": [{{"name": "", "scenario": "", "chatbot_role": "", "expected_outcome": "", "user_turns": [""]}}]}}"""

SHORT_PART = """
   Also add {n_short} more with category "fact": a single-fact question whose "expected_answer" is at
   most 15 words (a number, a name, a short phrase) taken from the material."""

FACT_MAX_WORDS = 15


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + " …"


def write_dataset(judge, answers: list[tuple[str, str]], pages: list[tuple[str, str]],
                  persona: str = "", *, n_goldens: int = GOLDENS_WANTED, n_scenarios: int = SCENARIOS_WANTED,
                  n_short: int = 0, existing: Sequence[str] = ()) -> tuple[list[dict], list[dict]]:
    """Golden answers {question, expected_answer, categories} and scenarios
    {name, scenario, chatbot_role, expected_outcome, user_turns} for this chatbot.
    Questions that repeat `existing` or each other are dropped, as are `fact`
    rows whose answer is longer than 15 words. At most n_short `fact` rows and
    n_goldens * 2 others are returned."""
    per_page = PROMPT_TEXT_CHARS // max(1, len(pages))
    known = [q for q in existing if q and q.strip()]
    avoid = ("Do not repeat these questions that already exist:\n"
             + "\n".join(f"- {_clip(q, 120)}" for q in known[:30]) + "\n" if known else "")
    data = ask_json(judge, WRITE_PROMPT.format(
        role=f"Its intended role: {persona.strip()}\n" if persona.strip() else "",
        interview="\n".join(f"Q: {q}\nA: {_clip(a, 1200)}" for q, a in answers) or "(it did not answer)",
        pages="\n\n".join(f"[{url}]\n{_clip(text, per_page)}" for url, text in pages) or "(none found)",
        n_goldens=n_goldens, n_scenarios=n_scenarios, avoid=avoid,
        short=SHORT_PART.format(n_short=n_short) if n_short > 0 else ""))
    seen = list(known)
    facts, others = [], []
    for row in data.get("goldens") or []:
        if not isinstance(row, dict):
            continue
        question, expected = str(row.get("question", "")).strip(), str(row.get("expected_answer", "")).strip()
        if not question or not expected:
            continue
        category = str(row.get("category", "")).strip()
        is_fact = category.lower() == "fact"
        if is_fact and len(expected.split()) > FACT_MAX_WORDS:
            continue
        if is_duplicate(question, seen):
            continue
        seen.append(question)
        (facts if is_fact else others).append({"question": question, "expected_answer": expected,
                                               "categories": [category] if category else []})
    scenarios = []
    for row in data.get("scenarios") or []:
        if not isinstance(row, dict):
            continue
        turns = [str(t).strip() for t in row.get("user_turns") or [] if str(t).strip()]
        name = str(row.get("name", "")).strip()
        if name and len(turns) >= 2:
            scenarios.append({"name": name, "scenario": str(row.get("scenario", "")).strip(),
                              "chatbot_role": str(row.get("chatbot_role", "")).strip() or persona.strip(),
                              "expected_outcome": str(row.get("expected_outcome", "")).strip(),
                              "user_turns": turns[:4]})
    return others[:n_goldens * 2] + facts[:n_short], scenarios[:n_scenarios * 2]


GROUNDED_PROMPT = """Below is the material about a chatbot and a list of test rows (question + expected answer).
For each row in order, say "supported" when the material backs the expected answer (or, for a behaviour
description, when it does not contradict or invent anything beyond the material), else "unsupported".

Material:
{material}

Rows:
{rows}

Return only JSON: {{"verdicts": ["supported", "unsupported"]}} with exactly {n} verdicts, in row order."""


def check_grounded(judge, rows: list[dict], material: str) -> list[bool]:
    """One True/False per row: does the material support its expected answer?
    No material, a bad reply or the wrong number of verdicts means all False."""
    if not rows:
        return []
    if not (material or "").strip():
        return [False] * len(rows)
    listing = "\n".join(f"{i + 1}. Q: {_clip(str(r.get('question', '')), 300)}\n   Expected: "
                        f"{_clip(str(r.get('expected_answer', '')), 400)}" for i, r in enumerate(rows))
    try:
        data = ask_json(judge, GROUNDED_PROMPT.format(material=_clip(material, PROMPT_TEXT_CHARS),
                                                      rows=listing, n=len(rows)))
    except (BudgetSpent, RunCancelled):
        raise
    except Exception:  # noqa: BLE001 - a bad judge reply just means nothing is trusted
        return [False] * len(rows)
    verdicts = data.get("verdicts")
    if not isinstance(verdicts, list) or len(verdicts) != len(rows):
        return [False] * len(rows)
    return [str(v).strip().lower() == "supported" for v in verdicts]


REPAIR_PROMPT = """These conversation scenarios for a chatbot need repair.
{tasks}
Base everything on the material and the bot's stated scope, never on what the bot might reply.

Chatbot: {persona}
Material:
{material}

Scenarios:
{scenarios}

Return only JSON: {shape}"""

REPAIR_OUTCOMES = """- Scenarios {ids} have no expected outcome. For each, write one sentence saying what the user
  should end up with."""
REPAIR_CONTEXTS = """- Scenarios {ids} need source passages. For each, copy up to {n} passages from the material, word
  for word, that its user messages are about."""
CONTEXT_PASSAGES = 3
PASSAGE_CHARS = 1500


def _squash(text: str) -> str:
    return " ".join(str(text).split()).lower()


def repair_scenarios(judge, scenarios: list[dict], persona: str, material: str,
                     need_context: Sequence[str] = ()) -> list[tuple[str, dict]]:
    """(id, fields) for scenarios with an empty chatbot_role (filled from the
    persona, no judge call) or empty expected_outcome, and, for the ids in
    need_context, a `context` of up to 3 passages from the material. Outcomes
    and contexts come from one judge call; a passage is kept only when it
    appears in the material (ignoring case and spacing), cut to 1500 chars.
    A field that already has a value is never returned."""
    fixes: dict[str, dict] = {}
    persona = (persona or "").strip()
    wanted_context = {str(i) for i in need_context}
    needs_outcome, needs_context = [], []
    for sc in scenarios:
        sid = str(sc.get("id", ""))
        if not sid:
            continue
        if persona and not str(sc.get("chatbot_role") or "").strip():
            fixes.setdefault(sid, {})["chatbot_role"] = persona
        if not str(sc.get("expected_outcome") or "").strip():
            needs_outcome.append(sc)
        if sid in wanted_context and not sc.get("context"):
            needs_context.append(sc)
    if needs_outcome or needs_context:
        listed = {str(sc["id"]): sc for sc in needs_outcome + needs_context}
        listing = "\n".join(f"[{sid}] {sc.get('name', '')}: " + " | ".join(map(str, sc.get("user_turns") or []))
                            for sid, sc in listed.items())
        tasks, shape = [], []
        if needs_outcome:
            tasks.append(REPAIR_OUTCOMES.format(ids=", ".join(str(sc["id"]) for sc in needs_outcome)))
            shape.append('"outcomes": {"<scenario id>": "<one sentence>"}')
        if needs_context:
            tasks.append(REPAIR_CONTEXTS.format(ids=", ".join(str(sc["id"]) for sc in needs_context),
                                                n=CONTEXT_PASSAGES))
            shape.append('"contexts": {"<scenario id>": ["<passage>"]}')
        try:
            data = ask_json(judge, REPAIR_PROMPT.format(tasks="\n".join(tasks), persona=persona or "(unknown)",
                                                        material=_clip(material or "(none)", PROMPT_TEXT_CHARS),
                                                        scenarios=listing, shape="{" + ", ".join(shape) + "}"))
        except (BudgetSpent, RunCancelled):
            raise
        except Exception:  # noqa: BLE001 - nothing written, the role fixes still count
            data = {}
        outcomes = data.get("outcomes")
        if isinstance(outcomes, dict):
            for sc in needs_outcome:
                text = str(outcomes.get(str(sc["id"]), "")).strip()
                if text:
                    fixes.setdefault(str(sc["id"]), {})["expected_outcome"] = text
        contexts = data.get("contexts")
        source = _squash(material or "")
        if isinstance(contexts, dict):
            for sc in needs_context:
                found = contexts.get(str(sc["id"]))
                passages = [str(p).strip()[:PASSAGE_CHARS] for p in found
                            if str(p).strip() and _squash(p) in source] if isinstance(found, list) else []
                if passages:
                    fixes.setdefault(str(sc["id"]), {})["context"] = passages[:CONTEXT_PASSAGES]
    return list(fixes.items())
