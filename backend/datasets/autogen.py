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

from urllib.parse import urlsplit

from backend.judges.ask import ask_json
from backend.rag.fetch import fetch_page_text

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


def interview(target) -> list[tuple[str, str]]:
    """The chatbot's own answers about what it does; [] when it cannot answer."""
    answers = []
    for question in INTERVIEW:
        try:
            reply = target.chat(question).reply
        except Exception:  # noqa: BLE001 - an unreachable bot just means no interview
            break
        if reply and reply.strip():
            answers.append((question, reply.strip()))
    return answers


WRITE_PROMPT = """You are building a test dataset for a chatbot so that it can be evaluated.
Learn what this chatbot is from the material below, then write test data that fits it.
{role}
What the chatbot said when asked about itself:
{interview}

Its help / FAQ / documentation pages:
{pages}

Write:
1. "goldens": {n_goldens} realistic questions real users would ask THIS chatbot, covering its main
   topics, one or two it should decline or redirect, and one edge case. For each, "expected_answer"
   describes what a good reply does (facts only when the pages state them; otherwise the behaviour,
   e.g. "Explains how to start a return and mentions the 30-day window"). Add a one-word "category".
2. "scenarios": {n_scenarios} multi-turn conversations a real user would have with it. Each has
   "name", "scenario" (one sentence), "chatbot_role", "expected_outcome" (what the user should end up
   with) and "user_turns" (2 to 4 user messages, later ones building on earlier ones, at least one
   referring back to something said before).

Never invent policies, prices or facts the material does not support. Return only JSON:
{{"goldens": [{{"question": "", "expected_answer": "", "category": ""}}],
  "scenarios": [{{"name": "", "scenario": "", "chatbot_role": "", "expected_outcome": "", "user_turns": [""]}}]}}"""


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + " …"


def write_dataset(judge, answers: list[tuple[str, str]], pages: list[tuple[str, str]],
                  persona: str = "") -> tuple[list[dict], list[dict]]:
    """Golden answers {question, expected_answer, categories} and scenarios
    {name, scenario, chatbot_role, expected_outcome, user_turns} for this chatbot."""
    per_page = PROMPT_TEXT_CHARS // max(1, len(pages))
    data = ask_json(judge, WRITE_PROMPT.format(
        role=f"Its intended role: {persona.strip()}\n" if persona.strip() else "",
        interview="\n".join(f"Q: {q}\nA: {_clip(a, 1200)}" for q, a in answers) or "(it did not answer)",
        pages="\n\n".join(f"[{url}]\n{_clip(text, per_page)}" for url, text in pages) or "(none found)",
        n_goldens=GOLDENS_WANTED, n_scenarios=SCENARIOS_WANTED))
    goldens = []
    for row in data.get("goldens") or []:
        if not isinstance(row, dict):
            continue
        question, expected = str(row.get("question", "")).strip(), str(row.get("expected_answer", "")).strip()
        if question and expected:
            category = str(row.get("category", "")).strip()
            goldens.append({"question": question, "expected_answer": expected,
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
    return goldens[:GOLDENS_WANTED * 2], scenarios[:SCENARIOS_WANTED * 2]
