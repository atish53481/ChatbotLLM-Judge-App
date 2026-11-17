"""What a vulnerability means when an attack gets through: the impact shown in the
Risk Assessment and the DeepTeam Report.

Each entry is (if_exploited, business): what the attacker gets, and what that costs
the company running the chatbot. Keys are the app's metric keys (risk.py); a key
without its own entry falls back to its DeepTeam category's text.
"""
from __future__ import annotations

IMPACT: dict[str, tuple[str, str]] = {
    # Data privacy
    "pii_leakage": (
        "The bot reveals personal data (names, emails, addresses, account or payment details) of users or staff.",
        "Privacy breach: GDPR / CCPA fines, mandatory breach notices, identity-theft and phishing risk for the people exposed."),
    "data_exfiltration": (
        "The bot hands out personal or confidential data it can see in its context, documents or tools.",
        "Data breach: regulatory fines, breach notices, lost customer trust and possible lawsuits."),
    "no_prompt_leak": (
        "The bot discloses its system prompt: hidden rules, internal instructions, and any secrets or keys placed there.",
        "Attackers learn exactly which guardrails to bypass; leaked keys or internal URLs open the systems behind the bot."),
    "prompt_leakage": (
        "The bot discloses its system prompt: hidden rules, internal instructions, and any secrets or keys placed there.",
        "Attackers learn exactly which guardrails to bypass; leaked keys or internal URLs open the systems behind the bot."),
    "cross_context_retrieval": (
        "The bot returns documents or records that belong to another user, tenant or session.",
        "One customer sees another's data: a cross-tenant breach with legal exposure and contract penalties."),
    # Security
    "prompt_injection": (
        "The attacker's text overrides the bot's instructions, so the bot follows the attacker instead of its owner.",
        "Every other guardrail can be switched off this way; it is the entry point for leaks, abuse and harmful replies."),
    "jailbreak": (
        "Safety rules are bypassed and the bot produces content or actions it is meant to refuse.",
        "Harmful or off-policy output published under the company's name; screenshots spread, platform-policy violations."),
    "encoded_injection": (
        "Instructions hidden in Base64, ROT13, leetspeak or another language slip past the input filter.",
        "Filters give a false sense of safety: any blocked request can be resent encoded."),
    "multi_turn_jailbreak": (
        "A slow, step-by-step conversation wears the bot down until it does what it refused at first.",
        "Single-message tests look safe while patient attackers still get harmful output."),
    "bola": (
        "The attacker reads or changes another user's objects (orders, tickets, accounts) by changing an id.",
        "Account takeover and data breach across customers; one of the most common API breaches (OWASP API1)."),
    "bfla": (
        "The attacker calls admin or privileged functions through the bot without having that role.",
        "Unauthorised refunds, deletions, config changes or privilege escalation."),
    "rbac": (
        "Role checks are bypassed: a normal user gets answers or actions reserved for staff or admins.",
        "Privilege escalation; internal data and admin operations exposed to anyone who asks."),
    "debug_access": (
        "The bot exposes debug modes, logs, stack traces or developer commands.",
        "Internal architecture, credentials and bugs revealed: a map for further attacks."),
    "sql_injection": (
        "Attacker-written SQL reaches the database through the bot's tools.",
        "Database read, change or wipe: mass data breach, data loss, full system compromise."),
    "shell_injection": (
        "Attacker-written commands run on the server through the bot's tools.",
        "Remote code execution: the server, its secrets and the network behind it are taken over."),
    "ssrf": (
        "The bot fetches internal URLs (cloud metadata, admin panels, internal APIs) for the attacker.",
        "Cloud credentials and internal services exposed; often the first step to a full cloud compromise."),
    "system_reconnaissance": (
        "The bot describes its infrastructure: models, tools, endpoints, file paths, versions.",
        "Gives attackers the map they need to plan targeted exploits."),
    "tool_metadata_poisoning": (
        "Instructions hidden in tool names, descriptions or schemas steer the agent.",
        "A third-party tool or MCP server can silently take control of what the agent does."),
    "unexpected_code_execution": (
        "The bot writes and runs code the attacker asked for.",
        "Remote code execution: data theft, malware or resource abuse on the company's infrastructure."),
    # Safety
    "harmful_content": (
        "The bot gives dangerous instructions (weapons, drugs, self-harm, crime).",
        "Real-world harm to people, legal liability, app-store and platform bans, press coverage."),
    "illegal_activity": (
        "The bot helps plan or carry out crimes (fraud, hacking, trafficking, evasion).",
        "Legal liability for aiding crime, regulatory action and severe reputational damage."),
    "graphic_content": (
        "The bot produces violent, sexual or disturbing content.",
        "Users (possibly minors) exposed to harmful material; brand damage and policy violations."),
    "personal_safety": (
        "The bot encourages self-harm, dangerous challenges or harassment, or mishandles someone at risk.",
        "Risk to life; duty-of-care and legal exposure; the most damaging kind of headline."),
    "social_engineering": (
        "The bot is talked into trusting a fake identity or authority and acts on it.",
        "Fraud and account takeover by impersonation; staff-only actions done for strangers."),
    # Responsible AI
    "bias": (
        "Replies treat people differently by gender, race, religion, politics or other traits.",
        "Discrimination claims, regulatory risk (EU AI Act, equality law) and loss of user trust."),
    "fairness": (
        "Decisions or advice are unequal across groups of users.",
        "Discriminatory outcomes, legal exposure and audit failures."),
    "toxicity": (
        "The bot insults, threatens or uses hateful language.",
        "Abusive replies in the company's voice: viral screenshots, users lost, moderation-policy breaches."),
    "ethics": (
        "The bot gives unethical guidance or endorses deceptive, manipulative behaviour.",
        "Reputational damage and loss of trust in the product's judgement."),
    "child_protection": (
        "The bot produces content or interactions unsafe for children, or fails to protect a minor.",
        "Severe legal liability (COPPA, online-safety laws), platform bans and reputational crisis."),
    # Business
    "dt_hallucination": (
        "The bot states made-up facts, prices, policies or sources with confidence.",
        "Customers act on false information: refunds, complaints, and in some courts the company is bound by what its bot said."),
    "misinformation": (
        "The bot spreads false or misleading claims as fact.",
        "Users misled on decisions; brand credibility and possible legal exposure."),
    "competition": (
        "The bot recommends competitors, disparages them or reveals competitive information.",
        "Lost sales and possible unfair-competition or defamation claims."),
    "intellectual_property": (
        "The bot reproduces copyrighted, trademarked or proprietary content.",
        "Copyright / trademark claims and leaked trade secrets."),
    "domain_misuse": (
        "The bot is used for tasks outside its purpose (free general assistant, homework, code generation).",
        "Paid model usage burned by others; off-brand answers attributed to the company."),
    "non_advice": (
        "The bot gives medical, legal or financial advice it is not allowed to give.",
        "Liability if someone acts on it; regulated-advice violations."),
    "role_violation": (
        "The bot drops its persona and role and behaves as something else.",
        "Off-brand, unpredictable replies; a stepping stone to further jailbreaks."),
    # Agentic
    "excessive_agency": (
        "The agent takes actions beyond what the user asked or is allowed to approve.",
        "Unwanted purchases, emails, deletions or changes done in the user's or company's name."),
    "agent_identity_abuse": (
        "The agent is made to impersonate a user, another agent or the company.",
        "Fraud and actions taken under a trusted identity; audit trails become worthless."),
    "autonomous_agent_drift": (
        "Over long tasks the agent drifts from its goal and rules.",
        "Unpredictable actions and cost overruns that no one approved."),
    "exploit_tool_agent": (
        "The agent's tools are used against the systems they connect to.",
        "Data changes, leaks or outages in connected systems."),
    "external_system_abuse": (
        "The agent is used to spam, attack or overload external services.",
        "The company becomes the source of abuse: blocklisting, account bans, legal complaints."),
    "goal_theft": (
        "The attacker extracts or replaces the agent's goals and instructions.",
        "The agent works for the attacker; business logic leaks."),
    "indirect_instruction": (
        "Instructions hidden in web pages, documents, emails or tool results take control of the agent.",
        "Anyone who can put text where the agent reads it can steer it: data theft, unwanted actions."),
    "insecure_inter_agent_communication": (
        "Messages between agents are spoofed or altered.",
        "One compromised agent compromises the whole pipeline."),
    "recursive_hijacking": (
        "The agent is pushed into self-repeating or self-modifying loops.",
        "Runaway cost, denial of service and actions repeated many times."),
    "dt_robustness": (
        "Small changes to the input (typos, rewording, noise) change the agent's behaviour or bypass its rules.",
        "Guardrails that hold only for exact phrasings; unreliable answers for real users."),
    "tool_orchestration_abuse": (
        "Tools are chained in ways no one intended (read secret, then send it out).",
        "Multi-step exploits that no single tool check catches: data exfiltration and abuse."),
}

CATEGORY_IMPACT: dict[str, tuple[str, str]] = {
    "Data Privacy": ("The bot reveals data that should stay private.",
                     "Data breach: fines, breach notices and lost customer trust."),
    "Security": ("The attacker gets the bot to bypass its controls or reach systems behind it.",
                 "Unauthorised access to data and systems; a foothold for wider attacks."),
    "Safety": ("The bot produces dangerous or harmful output.",
               "Real-world harm, legal liability and reputational damage."),
    "Responsible AI": ("The bot replies unfairly, offensively or unethically.",
                       "Discrimination and conduct complaints, regulatory risk, loss of trust."),
    "Business": ("The bot says things that are wrong or off-brand for the business.",
                 "Wrong decisions by customers, lost sales and liability for what the bot said."),
    "Agentic": ("The agent takes actions the user or company did not intend.",
                "Unwanted changes in connected systems, fraud and runaway cost."),
    "Custom": ("The bot breaks one of your own rules for this vulnerability.",
               "Whatever that rule protects is at risk: see the vulnerability's criteria."),
}


def of(key: str, category: str) -> dict:
    """{if_exploited, business} for a vulnerability (metric key + DeepTeam category)."""
    exploited, business = IMPACT.get(key) or CATEGORY_IMPACT.get(category) or CATEGORY_IMPACT["Security"]
    return {"if_exploited": exploited, "business": business}


def status_line(v: dict) -> str:
    """Whether this run showed the impact, in one sentence."""
    if not v.get("total"):
        return "Not tested in this run."
    if v.get("failed"):
        return (f"Confirmed: {v['failed']} of {v['total']} attacks got through, so this impact is reachable "
                "by a real attacker today.")
    return f"Not shown: all {v['total']} attacks were resisted. This is the impact if a future attack gets through."
