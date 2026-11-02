"""Stop hook: keeps the workflow docs in step with the code.

Claude Code runs this when Claude is about to finish a turn (.claude/settings.json).
When a file that defines how the app behaves (backend code, the web app, the start
scripts, the shipped datasets, CI) is newer than the workflow docs, it blocks the
stop once and asks Claude to update the docs and republish the workflow page
(CLAUDE.md, "Keeping the docs current").

The docs count as updated when docs/WORKFLOW.md, docs/workflow.html or the local
stamp .claude/docs-reviewed is newer than every such file. A change that needs no
doc update is acknowledged with:

    python .claude/hooks/docs_guard.py --ack
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOCS = ("docs/WORKFLOW.md", "docs/workflow.html")
STAMP = ".claude/docs-reviewed"
WORKFLOW_URL = "https://claude.ai/artifact/JxkuyFH5iLH8ZdzoszZ4Qv"
# A fresh checkout writes files a moment apart: ignore differences this small.
SLACK_S = 2.0
SHOWN = 8


def documented(path: str) -> bool:
    """Files whose changes the workflow docs describe."""
    if "/__pycache__/" in path or path.startswith(("tests/", "docs/", "01_Chatbot_Shopeasy_chatbot/")):
        return False
    if path.startswith("backend/"):
        return path.endswith(".py") or (path.startswith("backend/datasets/") and path.endswith(".default.json"))
    if path.startswith("ChatbotExtension/"):
        return path.endswith((".js", ".html", ".css")) and not path.startswith("ChatbotExtension/lib/vendor/")
    if path.startswith(".github/workflows/"):
        return path.endswith((".yml", ".yaml"))
    return "/" not in path and (path.endswith((".bat", ".ps1")) or path in ("requirements.txt", ".env.example"))


def mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def changed_since_docs() -> list[str]:
    listed = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        capture_output=True, check=True,
    ).stdout.decode("utf-8", "replace")
    docs_at = max(mtime(ROOT / p) for p in (*DOCS, STAMP))
    files = sorted({p for p in listed.split("\0") if p and documented(p)})
    return [p for p in files if mtime(ROOT / p) > docs_at + SLACK_S]


def main() -> int:
    if "--ack" in sys.argv[1:]:
        stamp = ROOT / STAMP
        stamp.parent.mkdir(exist_ok=True)
        stamp.write_text(time.strftime("%Y-%m-%d %H:%M:%S") + "\n", encoding="utf-8")
        print(f"Docs marked as reviewed ({STAMP}).")
        return 0
    try:
        event = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        event = {}
    if event.get("stop_hook_active"):
        return 0  # Claude is already continuing because of this hook: never loop
    try:
        changed = changed_since_docs()
    except (OSError, subprocess.CalledProcessError):
        return 0  # no git here: nothing to compare against
    if not changed:
        return 0
    listed = ", ".join(changed[:SHOWN]) + (f" and {len(changed) - SHOWN} more" if len(changed) > SHOWN else "")
    reason = (
        f"Code changed after the workflow docs were last updated: {listed}. Before finishing, bring the docs "
        "in line with this change (CLAUDE.md, 'Keeping the docs current'): docs/WORKFLOW.md and "
        f"docs/workflow.html (bump its footer date), then republish docs/workflow.html to {WORKFLOW_URL} "
        "(Artifact tool: read that URL first, then publish with url set to it); also CLAUDE.md, README.md and "
        "docs/ADVANCED_TOOLS.md where they describe what changed. If nothing documented changed, run: "
        "python .claude/hooks/docs_guard.py --ack"
    )
    print(json.dumps({"decision": "block", "reason": reason}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
