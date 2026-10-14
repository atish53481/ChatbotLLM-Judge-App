# LLM Evaluation Notes for QA & SDETs

In normal testing you write `assertEquals(expected, actual)`. In LLM testing, you can't: the answer changes wording every time. Instead, a **judge model** gives each answer a score from 0 to 1, and the test passes when the score reaches a **threshold**.

Facts below are checked against DeepEval 4.2 (the version this project uses).

## Why LLM eval

Most products now ship some AI: a chatbot, a RAG app (answers from your documents) or an AI agent. Real users see those answers, so QA has to sign them off.

| | Traditional testing | LLM evaluation |
| --- | --- | --- |
| Output | Same input → same output | Same input → different wording each time |
| Check | expected == actual | score ≥ threshold |
| Oracle | Hard-coded assertion | A judge: a strong LLM, rules, or a human |
| A failure looks like | Wrong value, exception | Fluent, confident, but wrong or unsafe answer |

What goes wrong without evals:

- **Rewording breaks tests**: `assertEquals` fails on harmless changes. Use scores with thresholds.
- **No single right answer**: "summarize this" has many good answers. Score quality on several angles.
- **Hallucinations**: the model invents facts and sounds sure. Check the answer against a source of truth.
- **Safety**: toxic or biased replies, jailbreaks and prompt injection are real failures.
- **Cost and speed**: token cost and response time are test results too.

When to run evals:

1. **Model change**: swapping one model for another (or a new version) needs a full recheck.
2. **Before a release**: gate the answers before they reach users.
3. **Missing guardrails**: prove the bot stays safe and on topic. Real example: a fast-food support bot that happily wrote Python code when asked.

What you can evaluate: chatbots, RAG apps, AI agents (an LLM plus memory and tools), voice assistants and copilots.

**Red teaming** is the attack side: you send harmful or tricky prompts ("ignore your rules", "help me make a weapon") and check that the bot refuses. See [Red teaming and security](#red-teaming-and-security).

The tool can change; the ideas stay. These notes use **DeepEval**: open source, 50+ metrics, runs locally, Python 3.9+.

## Key terms

Every DeepEval idea maps to something QA already knows.

| Term | Meaning | QA equivalent |
| --- | --- | --- |
| Prompt | What goes into the LLM: system instructions, user message, maybe retrieved text | Request payload |
| `actual_output` | The LLM's answer that we test | Actual result |
| `expected_output` | The correct answer, written by a human | Expected result |
| Golden dataset | A list of questions with expected answers, run on every change | Regression suite |
| Judge / evaluator | Whatever gives the score: rules, an LLM or a human | Test oracle |
| LLM-as-a-judge | A strong LLM that scores another LLM's answer | Validation service |
| Hallucination | A confident statement that is not true or not in the given facts | Wrong data shown as right |
| Faithfulness | The answer only says what the retrieved text supports | Data integrity check |
| Answer relevancy | The answer actually responds to the question | Requirement coverage |
| `context` | Facts you know are true (you supply them) | Reference data |
| `retrieval_context` | The text the app's search actually returned | Data the app fetched |
| Threshold | The pass mark, from 0.0 to 1.0 | Acceptance criteria |
| `criteria` (G-Eval) | Your rule in plain English, for the judge to apply | Assertion logic |
| `evaluation_params` (G-Eval) | Which fields the judge reads | Which fields to assert on |
| Trace / span | A recording of one run / one step in it (LLM call, tool, search) | Request log / log line |
| Eval harness | The code that runs the dataset through the app and the metrics | Test framework |

## How DeepEval works

The golden dataset gives the question and the truth. The app gives the answer. A separate judge LLM scores them together as one **test case**.

```mermaid
flowchart LR
    G["Golden dataset<br/>input, expected_output,<br/>context (ground truth)"] --> A["Chatbot / RAG / agent<br/>returns actual_output<br/>and retrieval_context"]
    A --> T["LLMTestCase<br/>bundles every field"]
    T --> J["Metric + judge LLM<br/>e.g. Faithfulness, scored<br/>by a separate model"]
    J --> S["Score 0.0 to 1.0<br/>plus a written reason"]
    S --> D{"score at or<br/>above threshold?"}
    D -- yes --> P["PASS"]
    D -- no --> F["FAIL"]
```

In code: build an `LLMTestCase`, pick metrics with thresholds, then call `assert_test` (fails a pytest test) or `evaluate` (scores a whole batch). You can test the final answer only (black box), one step (component), or an agent's whole run (trajectory).

## Metrics catalogue

Pick metrics by what you are testing. A chatbot needs answer quality and safety. A RAG app adds retrieval metrics. An agent adds tool and plan metrics.

In DeepEval 4, **every metric scores the same way: 1 = pass, 0 = fail, and the threshold is a minimum**. (Older versions scored Hallucination, Bias, Toxicity and PII Leakage the other way round; see [Thresholds](#thresholds).)

| Use case | Metric | Question it answers | Needs |
| --- | --- | --- | --- |
| Answer quality | Answer Relevancy | Did it answer the question? | input, actual_output |
| Answer quality | Faithfulness | Did it stick to the retrieved text? | + retrieval_context |
| Answer quality | Hallucination | Did it contradict known facts? | + context |
| Answer quality | Correctness (G-Eval) | Does it match the expected answer? | + expected_output |
| Answer quality | Summarization | Is the summary accurate and complete? | input (source), actual_output |
| Answer quality | JSON Correctness | Is the output valid JSON in the right shape? | actual_output, schema |
| RAG retrieval | Contextual Precision | Were the useful chunks ranked first? | + retrieval_context, expected_output |
| RAG retrieval | Contextual Recall | Was everything needed retrieved? | + retrieval_context, expected_output |
| RAG retrieval | Contextual Relevancy | Is the retrieved text about the question? | + retrieval_context |
| Safety | Bias | Is the answer biased (gender, race, politics…)? | actual_output |
| Safety | Toxicity | Is it rude, insulting or hateful? | actual_output |
| Safety | PII Leakage | Does it expose personal data? | actual_output |
| Safety | Non-Advice | Does it avoid medical, legal or financial advice? | actual_output |
| Safety | Misuse | Does it refuse jobs outside its purpose? | actual_output |
| Safety | Role Violation | Does it stay in its assigned role? | actual_output |
| Conversation | Conversation Completeness | Were the user's needs met across turns? | multi-turn test case |
| Conversation | Knowledge Retention | Does it remember earlier turns? | multi-turn test case |
| Conversation | Conversation Relevancy / Role Adherence / Topic Adherence | Does each turn stay relevant, in role and on topic? | multi-turn test case |
| Agent | Task Completion | Did the agent finish the task? | trace |
| Agent | Tool Correctness / Argument Correctness | Right tools, right arguments? | tools_called, expected_tools / trace |
| Agent | Plan Quality / Plan Adherence | Was the plan good, and was it followed? | trace |
| Agent | Step Efficiency | Did it avoid useless steps? | trace |
| Agent | MCP metrics | Did it use MCP servers and tools correctly? | MCP test case |
| Custom | G-Eval / Arena G-Eval / DAG | Your own rule, a side-by-side comparison, or a decision tree | your criteria |

DeepEval also has multimodal (image) metrics and turn-level RAG metrics for chats.

**G-Eval** is the escape hatch: write the rule in plain English as `criteria`, choose `evaluation_params`, and the judge scores against it. Use it for anything built-in metrics miss: brand tone, refusal policy, "never reveal the system prompt".

Worked example: the question is "Can you refund my item, I don't like it", and the expected answer is "no refund after 30 days". The chatbot replies "Since your order #23221 is over 30 days old, we can't refund you". The judge compares the two and scores correctness and relevancy.

## Thresholds

A threshold turns a score into pass or fail, like a tolerance band. In DeepEval 4 the score must be **at or above** the threshold for every metric.

```python
score = judge_llm_evaluates(test_case)        # e.g. 0.82
result = "PASS" if score >= threshold else "FAIL"   # 0.82 >= 0.70 -> PASS
```

**The direction trap.** Older DeepEval versions (and many tutorials) scored Hallucination, Bias, Toxicity and PII Leakage as a *violation rate*: 0 = clean, lower is better, threshold is a maximum. DeepEval 4 flipped them to 1 = clean. If you copy an old threshold like "Hallucination 0.2" into DeepEval 4, it now means the opposite. Always check your version.

The LLM Judge app keeps the easy-to-read violation-rate view for those four: it shows **1 − DeepEval's score**, with the threshold as a maximum (≤). The tables below use that view.

A pass is not proof the answer is right; it means the judge scored it above the bar. A fail is a strong signal. So combine several metrics, check some results by hand, and tune thresholds over time.

| Metric | Shown as | Good | Bad | Threshold 0.7 / 0.5 means |
| --- | --- | --- | --- | --- |
| Answer Relevancy | higher is better | 0.95 | 0.30 | must be ≥ 0.7 |
| Faithfulness | higher is better | 0.98 | 0.20 | must be ≥ 0.7 |
| Contextual Precision / Recall | higher is better | 0.90 | 0.10 | must be ≥ 0.7 |
| Hallucination (app view) | lower is better | 0.05 | 0.90 | must be ≤ 0.5 |
| Toxicity (app view) | lower is better | 0.02 | 0.85 | must be ≤ 0.5 |
| Bias (app view) | lower is better | 0.03 | 0.75 | must be ≤ 0.5 |

Choosing a threshold in three steps:

1. **Start loose (0.5)**: run the suite and look at the scores you get. Like a performance baseline before setting SLAs.
2. **Match reality (0.6-0.7)**: if most scores sit between 0.75 and 0.90, set 0.7.
3. **Tighten for production (0.8-0.9)**: once the bot is stable, raise the bar.

Thresholds by environment (loose in dev, strict in prod). These are the app's presets:

| Environment | Answer Relevancy | Faithfulness | Hallucination | Toxicity | Correctness (G-Eval) |
| --- | --- | --- | --- | --- | --- |
| Local development | ≥ 0.5 | ≥ 0.5 | ≤ 0.6 | ≤ 0.6 | ≥ 0.5 |
| PR / feature branch | ≥ 0.7 | ≥ 0.7 | ≤ 0.5 | ≤ 0.4 | ≥ 0.6 |
| Staging / QA | ≥ 0.8 | ≥ 0.85 | ≤ 0.3 | ≤ 0.3 | ≥ 0.7 |
| Production | ≥ 0.85 | ≥ 0.9 | ≤ 0.2 | ≤ 0.2 | ≥ 0.8 |

The judge is an LLM too, so its scores wobble. Score important cases twice; a gap above about 0.15 means the judge is unstable, not that the bot got worse.

## Red teaming and security

Red teaming sends attacks and checks the bot refuses. A pass means the attack failed. Typical attacks and what a safe bot does:

| Probe | What it tries | A safe bot… |
| --- | --- | --- |
| Prompt Injection | "Ignore your instructions and…" | Resists attempts to override its instructions |
| Jailbreak | Persona swaps, "DAN", developer mode, "it's just a story" | Refuses, whatever the framing |
| Encoded Injection | The same attack in Base64, spaced letters or another language | Is not fooled by the encoding |
| Data Exfiltration | "List all customers", "print your knowledge base" | Refuses to dump records or internal data |
| Social Engineering | "I'm the admin", "the policy changed today" | Does not accept claimed authority or invented rules |
| Domain Misuse | Medical, legal or coding help from a shop bot | Stays inside its own domain |
| Non-Advice | Medical, financial or legal advice | Gives none |
| Role Violation | Pushing it to break its persona | Never breaks role |
| Harmful Content | Weapons, crime, malware, self-harm | Refuses to help |
| No Prompt Leak (G-Eval) | "Show me your system prompt" | Refuses without leaking its instructions |

These follow the OWASP Top 10 for LLM applications (prompt injection, sensitive data disclosure, excessive agency…). DeepTeam, DeepEval's sister project, automates attacks like these. The LLM Judge app ships all ten as ready-made probes (see below).

## Evaluating AI agents

An agent can fail in its thinking or in its actions, so test each layer, not only the final answer. The LLM plans and picks tools (reasoning); the tools run (action); they loop until the task is done or fails.

| Scope | What it scores | Use it for |
| --- | --- | --- |
| End-to-end | Only the final output | Overall answer quality |
| Trajectory | The whole run: reasoning, action, result | Planning, flow, task completion |
| Component | One step on its own, such as one tool choice | A specific decision |

| Layer | Metric | Catches |
| --- | --- | --- |
| Reasoning | PlanQualityMetric | Bad, incomplete or wasteful plans |
| Reasoning | PlanAdherenceMetric | The agent not following its own plan |
| Action | ToolCorrectnessMetric | Wrong tool |
| Action | ArgumentCorrectnessMetric | Wrong tool arguments |
| Execution | TaskCompletionMetric | Task not done |
| Execution | StepEfficiencyMetric | Extra, useless steps |

Tracing with `@observe` records each step as a span and adds no latency. Attach metrics to the step you want scored:

```python
from deepeval.tracing import observe

@observe(type="tool")
def search_flights(origin, destination, date):
    return [{"id": "FL123", "price": 450}]

@observe(type="llm", metrics=[tool_correctness, argument_correctness])
def call_llm(messages):
    return client.chat.completions.create(...)

@observe(type="agent")
def travel_agent(user_input):
    response = call_llm(messages)
    flights = search_flights(...)
    return booking_result
```

Run the agent over a dataset of goldens, scoring every trace:

```python
for golden in dataset.evals_iterator(metrics=[plan_quality, plan_adherence]):
    travel_agent(golden.input)
```

Common failures: bad plans (reasoning); wrong tool, bad arguments or wrong order (action); unfinished tasks or going off-task (execution). In production, send traces to Confident AI and score them in the background to catch problems early.

## Hands-on: first DeepEval test

Four steps: a virtual environment, DeepEval, a judge model, and one `LLMTestCase`. No account needed; only an API key for the judge.

1. Create a virtual environment and install DeepEval:

```bash
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install --upgrade pip
pip install -U deepeval requests
```

2. Point the judge at Groq. Groq speaks the OpenAI API, so register it as a local model. Careful: `deepeval set-grok` is xAI's Grok, not Groq.

```bash
deepeval set-local-model \
  --model openai/gpt-oss-120b \
  --base-url "https://api.groq.com/openai/v1" \
  --format json \
  --prompt-api-key
# or set the key yourself: export LOCAL_MODEL_API_KEY=<your Groq key>
```

Any OpenAI-compatible model works; change `--model`. **The judge must never be the chatbot under test.**

3. Write the test (`test_01_answer_relevancy.py`):

```python
from deepeval import assert_test
from deepeval.test_case import LLMTestCase
from deepeval.metrics import AnswerRelevancyMetric

def test_hello_world():
    test = LLMTestCase(
        input="What is 2+2?",
        actual_output="4",
        expected_output="4",
        context=["Basic arithmetic: perform it and give the result"],
    )
    metric = [AnswerRelevancyMetric(threshold=0.9)]
    assert_test(test, metric)
```

4. Run it:

```bash
deepeval test run test_01_answer_relevancy.py
```

The result table shows the case, metric, score, threshold, judge, reason and status. Expect `Answer Relevancy 1.0 (threshold=0.9) PASSED`.

In a real suite, `actual_output` comes from the chatbot, not a fixed string. Copy the chatbot's request from the browser (F12 → Network → Copy as cURL), send each golden question with `requests`, and read the reply. It usually looks like:

```bash
curl 'https://<chatbot-host>/api/bots/<bot-id>/chat' \
  -H 'content-type: application/json' \
  --data-raw '{"message":"<question>","visitorId":"<id>"}'
```

`assert_test` fails a pytest test; `evaluate(test_cases, metrics)` scores a batch without failing. Results can stay local, or go to the Confident AI dashboard after `deepeval login` (only the first project is free, so a local dashboard is cheaper).

The loop: **define what good means, choose metrics, build a dataset, run, improve**. In development, tune prompts and models against the scores. In production, monitor real traffic and add failures back to the golden dataset.

## Using the LLM Judge app

This project does all of the above for you, with no test code. It opens in your browser as a web app (it also works as a Chrome extension side panel). Both talk to a local backend at `http://127.0.0.1:8765`.

![LLM Judge web app: side panel on the left, dashboard on the right](images/app-overview.jpg)

### 1. Set up once

1. Put your judge key in `.env` in the project folder: `JUDGE_API_KEY=<key>` (Groq by default; see `.env.example` for model and URL).
2. Create the desktop button: in the project folder run
   `powershell -ExecutionPolicy Bypass -File .\create-desktop-shortcut.ps1`

### 2. Start

Double-click **LLM Judge** on the desktop (or `start-llm-judge.bat`). It starts the backend if it is not running and opens the app. The first start builds the Python environment and takes a few minutes; after that, about 10 seconds. To stop, close the minimized "LLM Judge" console window.

The **side panel** (left) is where you set things up and run. The **dashboard** (right) shows one card per metric with its latest score.

### 3. Add the chatbot to test

![Add a chatbot form](images/app-add-chatbot.png)

1. Open the chatbot's website, press **F12 → Network**, and send it a message.
2. Right-click the chat request → **Copy → Copy as cURL (bash)**.
3. In the side panel, open **Add a chatbot**, give it a name, and paste into **cURL command**. The app fills in the URL, headers, where your question goes (`{{message}}`) and where the reply is.
4. Press **Test**, check the reply, and save.

Testing a plain LLM API instead? Use **Or fill from an LLM API (OpenAI-compatible)**. Optional fields: **Retrieved context path** (enables RAG metrics with the bot's own sources), **Security probes** set and **Chatbot role** (used by the security metrics). If the site's login expires (401/403), paste a fresh cURL.

### 4. Golden answers

Each chatbot uses a golden set. In **Golden answers**, add or edit question / expected-answer pairs. **Generate from a document or web page** creates goldens from a PDF, Word file or help page. **Conversation scenarios** hold multi-turn tests.

### 5. Choose metrics and thresholds

![Metrics to run and threshold presets](images/app-metrics-thresholds.png)

- **Thresholds for**: pick a preset (Local Development, PR / Feature Branch, Staging / QA, Production) from the table above, or type your own number next to any metric.
- **Metrics to run**: tick metrics by group: Quality, Retrieval, Safety, Security, G-Eval, Conversational. `≥` means higher is better, `≤` lower is better.
- **Check judge consistency** scores every reply twice and warns when the judge is unstable (uses about 2× judge tokens).

### 6. Run and read results

Press **Run judge**, or **Run** on one dashboard card, or **Run all visible**. Runs continue on the backend even if you close the page. **Cases per run** limits how many goldens are sent, which saves tokens.

![Details panel of a metric](images/app-details.png)

Each card shows the run's average score against its threshold. **Details** shows:

- the verdict (e.g. "Average 1.00 ≥ 0.70 → pass"), judge model, tokens and duration;
- every case: question, actual answer, expected answer, score and the judge's reason (**Copy** puts it in a bug report);
- for failures, **how to improve**: the judge's worst reasons, what to fix in the chatbot, and what to check in the test.

**History** shows the score trend over runs. **Download report (PDF)** builds a printable summary for sign-off.

### What the app scores

25 metrics in six groups:

| Group | Metrics |
| --- | --- |
| Quality | Faithfulness, Answer Relevancy, Hallucination, Summarization |
| Retrieval | Contextual Precision, Contextual Recall, Contextual Relevancy |
| Safety | Bias, Toxicity, PII Leakage |
| Security | Prompt Injection, Jailbreak, Encoded Injection, Data Exfiltration, Social Engineering, Domain Misuse, Non-Advice, Role Violation, Harmful Content |
| G-Eval | No Prompt Leak, Correctness, Citation Quality, Helpfulness |
| Conversational | Conversation Completeness, Knowledge Retention |

## Integrations and tools landscape

DeepEval plugs into most agent frameworks for tracing and accepts almost any model as the judge, so you rarely need to change your app to test it.

| Category | Integrations | What they give you |
| --- | --- | --- |
| Python frameworks | LangChain, LangGraph, LlamaIndex, CrewAI, Pydantic AI, OpenAI Agents, OpenAI, Anthropic, Google ADK, AWS AgentCore, Strands | Traces of chains, agents, tools and retrievers, ready to score |
| TypeScript frameworks | LangChain, LangGraph, Mastra, Vercel AI SDK, OpenAI Agents, OpenAI | The same tracing for JS/TS apps |
| Judge models | OpenAI, Azure OpenAI, Anthropic, Gemini, Vertex AI, Amazon Bedrock, DeepSeek, Grok, Moonshot, OpenRouter, Portkey, LiteLLM, Ollama, vLLM, LM Studio | Any of these as the judge; anything OpenAI-compatible (Groq) via `set-local-model` |
| Voice agents (Python) | Vapi, ElevenLabs, LiveKit, Pipecat | Simulated calls against a voice agent |
| Speech models (Python) | Speech-to-text: OpenAI, Deepgram, AssemblyAI, ElevenLabs, Cartesia. Text-to-speech: OpenAI, ElevenLabs, Cartesia, Deepgram | Voice in and out for voice evals |
| Vector databases (Python) | Chroma, Weaviate, Qdrant, PGVector, Elasticsearch, Cognee | Retrieval testing inside RAG pipelines |
| Other | Hugging Face | DeepEval callbacks during training |

Other eval tools, and where each fits:

| Tool | Best for |
| --- | --- |
| DeepEval | Pytest-style tests for chatbots, RAG and agents; 50+ metrics; open source |
| DeepTeam | Automated red teaming (attacks) for LLM apps |
| RAGAS | RAG pipelines specifically |
| Promptfoo | Comparing prompts and responses side by side; red teaming |
| TruLens | Observability plus evaluation |
| OpenAI Evals | Benchmark-style evaluation |
| LangSmith | Full observability: traces, datasets, monitoring |

## Sources

- [DeepEval introduction](https://deepeval.com/docs/introduction)
- [DeepEval metrics introduction](https://deepeval.com/docs/metrics-introduction)
- [AI agent evaluation guide](https://deepeval.com/guides/guides-ai-agent-evaluation)
- [DeepEval tutorials](https://deepeval.com/tutorials/tutorial-introduction)
- [DeepEval integrations](https://deepeval.com/integrations)
- [DeepEval metrics source code](https://github.com/confident-ai/deepeval/tree/main/deepeval/metrics)
- [DeepTeam](https://www.trydeepteam.com/)
- [OWASP Top 10 for LLM Applications](https://genai.owasp.org/llm-top-10/)
- [RAGAS](https://www.ragas.io/)
- Course notes and slides from TheTestingAcademy
