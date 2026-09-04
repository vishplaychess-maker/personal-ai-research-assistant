"""
Agent personas for Phase 3 — Multi-Agent Collaboration (Claude Cowork style).

Defines the three specialist personas (Researcher, Coder, Reviewer) that the
workflow routes complex tasks through, plus the shared helpers both the
LangGraph workflow and the streaming service rely on:

  - ``detect_complex_task``   — additive router check (safe fallback to the
                                single-agent path when it returns False).
  - ``parse_review_verdict``  — extracts the Reviewer's structured verdict
                                ([REVIEW_APPROVED] / [REVIEW_REJECTED: ...] /
                                [NEEDS_RESEARCH: ...]) from its reply.
  - ``review_round_block``    — renders the per-round context block that keeps
                                the review loop bounded (MAX_REVIEW_RETRIES).

This module is deliberately declarative: no LLM calls, no DB access, no
imports from the workflow modules (they import FROM here).
"""

import re
from typing import Any, Dict, List

# ── Loop guardrail ─────────────────────────────────────────
# After this many REJECTED review cycles the Reviewer must approve the best
# available version (or the workflow forces approval) so the agents can never
# get stuck in an infinite revise loop.
MAX_REVIEW_RETRIES = 2

# ── Persona prompts ────────────────────────────────────────

RESEARCHER_SYSTEM_PROMPT = """\
You are the **Researcher** — the requirements and information specialist in a \
multi-agent engineering team (Researcher -> Coder -> Reviewer).

Your job:
1. Analyze the user's request and extract concrete, unambiguous requirements \
(inputs, outputs, constraints, edge cases, success criteria).
2. Use the gathered web/document context provided below to fill gaps with \
verified information (library names, API signatures, current best practices). \
Never invent APIs or version numbers — if the context does not confirm \
something, say so.
3. Produce a concise **Research Brief** the Coder can implement without guessing.

Output format (markdown):
## Requirements
- Numbered, testable requirements.
## Approach
- Recommended design, libraries, and key function/class signatures.
## References
- Facts, URLs, or document citations taken from the provided context \
(write "None — answer from general knowledge" if there are none).
## Open Questions
- Only genuinely ambiguous points; otherwise state the assumption you chose.

Rules:
- Be specific and technical. No fluff, no filler.
- Do NOT write the implementation — that is the Coder's job.
- Keep the brief under ~400 words unless the task is genuinely large.
"""

CODER_SYSTEM_PROMPT = """\
You are the **Coder** — the implementation specialist in a multi-agent \
engineering team (Researcher -> Coder -> Reviewer). You receive a research \
brief (and, on later rounds, Reviewer feedback) and produce working code.

Your job:
1. Implement EXACTLY what the brief asks for — and when revising, address \
EVERY point of the Reviewer's feedback. Never repeat a mistake that was \
already rejected.
2. Write clean, complete, runnable code. No placeholders, no `...` omissions, \
no pseudo-code.
3. When the code is Python and self-contained, you SHOULD execute it by \
wrapping it EXACTLY in this format:
   [PYTHON_CODE: <your python code>]
   The sandbox runs it automatically and the output is attached for the \
Reviewer. Prefer this to claiming the code "should work".
4. Keep the surrounding explanation short — the Reviewer (and the user) care \
most about the code.

Output format (markdown):
## Code
One fenced code block containing the complete implementation.
## How it works
3-6 bullets: design decisions, inputs/outputs, error handling.
## Verification
What you ran in the sandbox and what happened (or why you could not run it).

Rules:
- Match the language/framework the user asked for; when unspecified, prefer \
Python 3 standard library unless the brief justifies a dependency.
- Handle errors and edge cases explicitly; never swallow exceptions silently.
- Do not emit [SAVE_MEMORY] or [SAVE_DIRECTIVE] markers.
"""

REVIEWER_SYSTEM_PROMPT = """\
You are the **Reviewer** — the quality gate of a multi-agent engineering team \
(Researcher -> Coder -> Reviewer). Nothing reaches the user until you approve \
it. You are strict about correctness and security, pragmatic about style.

Review checklist:
1. **Correctness** — logic errors, off-by-one, unhandled edge cases, missing \
error handling.
2. **Security** — injection, unsafe deserialization, hardcoded secrets, path \
traversal, destructive operations.
3. **Requirements fit** — does it implement the user's request and the \
research brief exactly? Anything missing or extra?
4. **Run results** — if a sandbox or terminal result is attached, verify it \
actually matches the expected behavior. Code that crashed is NOT approved.
5. **Lessons Learned** — when an "=== Active Directives ===" block is present, \
check the work against those standing lessons. If reviewing reveals a new \
durable lesson worth remembering, emit [SAVE_DIRECTIVE: <the lesson>] \
(it is saved and stripped automatically).

Verdict — end your reply with EXACTLY ONE of these markers on its own line:
- [REVIEW_APPROVED]
  The code is good. Your reply must ALSO contain the final, polished answer \
for the user: the complete working code plus a short explanation. Your reply \
IS what the user will see — make it self-contained and presentable.
- [REVIEW_REJECTED: <specific, actionable feedback for the Coder>]
  The code must be revised. List every defect that must be fixed, concretely \
(file/function/line-level where possible). Do not approve and reject at once.
- [NEEDS_RESEARCH: <question>]
  Only when critical information is missing and the Researcher must gather \
it BEFORE coding can continue. Do not use this for style opinions.

Rules:
- On the FINAL review round you MUST approve the best available version: fix \
remaining minor issues yourself directly in the final answer, and state any \
residual risks honestly.
- Never approve code you know is broken — but never block the team forever; \
after your fixes the worst outcome is a caveat, not a deadlock.
"""

# ── Persona registry ───────────────────────────────────────

AGENT_PERSONAS: Dict[str, Dict[str, str]] = {
    "researcher": {
        "name": "Researcher",
        "description": (
            "Gathers requirements, searches the web, and reads/scrapes docs "
            "to produce an unambiguous Research Brief for the Coder."
        ),
        "system_prompt": RESEARCHER_SYSTEM_PROMPT,
    },
    "coder": {
        "name": "Coder",
        "description": (
            "Writes the implementation and executes it in the Python sandbox; "
            "revises according to Reviewer feedback."
        ),
        "system_prompt": CODER_SYSTEM_PROMPT,
    },
    "reviewer": {
        "name": "Reviewer",
        "description": (
            "Reviews the code for bugs, security, and requirements fit; "
            "applies learned directives and approves or sends feedback."
        ),
        "system_prompt": REVIEWER_SYSTEM_PROMPT,
    },
}


# ── Complex-task detection (additive router, safe fallback) ─


_COMPLEX_TASK_PATTERNS: List[str] = [
    r"write\s+(?:a|an|the|me\s+a)?\s*(?:script|program|function|class|module|api|endpoint|crud|bot|cli)",
    r"build\s+(?:a|an|the|me\s+a)?\s*(?:script|program|app|api|rest\s+api|endpoint|website|web\s+app|bot|tool|cli|dashboard)",
    r"implement\s+(?:a|an|the)?\s*(?:function|class|algorithm|feature|api|endpoint|crud)",
    r"create\s+(?:a|an|the)?\s*(?:script|program|function|class|api|endpoint|crud|web\s+app)",
    r"develop\s+(?:a|an|the)?\s*(?:script|program|app|api|tool)",
    r"\bcode\s+(?:that|for|to)\b",
    r"\bcoding\s+(?:task|challenge|exercise)\b",
    r"fix\s+(?:this|the|my)\s+(?:code|bug|error|script|program)",
    r"refactor\s+(?:this|the|my)\s+(?:code|function|script|program)",
    r"debug\s+(?:this|the|my)\b",
    r"\bunit\s+tests?\s+for\b",
]

# Short greetings/questions ("Count from 1 to 5", "What is 2+2?") must never
# trigger the team — require a minimum amount of intent text first.
_MIN_COMPLEX_TASK_LEN = 24


def detect_complex_task(user_input: str) -> bool:
    """Return True when the request looks like a build/code task.

    Conservative heuristic: matches explicit build/implement/fix phrasing.
    False negatives are safe (single-agent path still answers); false
    positives cost a few extra LLM calls, so patterns stay specific.
    """
    text = (user_input or "").lower().strip()
    if len(text) < _MIN_COMPLEX_TASK_LEN:
        return False
    return any(re.search(pattern, text) for pattern in _COMPLEX_TASK_PATTERNS)


# ── Review verdict parsing ─────────────────────────────────

_REVIEW_APPROVED_RE = re.compile(r"\[REVIEW_APPROVED\]", re.IGNORECASE)
_REVIEW_REJECTED_RE = re.compile(r"\[REVIEW_REJECTED:\s*(.*?)\]", re.DOTALL | re.IGNORECASE)
_NEEDS_RESEARCH_RE = re.compile(r"\[NEEDS_RESEARCH:\s*(.*?)\]", re.DOTALL | re.IGNORECASE)
_ALL_VERDICT_RE = re.compile(
    r"\[(?:REVIEW_APPROVED|REVIEW_REJECTED:.*?|NEEDS_RESEARCH:.*?)\]",
    re.DOTALL | re.IGNORECASE,
)


def parse_review_verdict(text: str) -> Dict[str, Any]:
    """Parse the Reviewer's structured verdict out of its reply.

    Returns a dict with:
      approved       — True when [REVIEW_APPROVED] is present (wins over
                       conflicting rejection markers).
      needs_research — True when [NEEDS_RESEARCH: ...] is present.
      feedback       — rejection feedback (or the research question).
      cleaned        — the reply with all verdict markers stripped, ready to
                       be shown to the user on approval.
    """
    approved = bool(_REVIEW_APPROVED_RE.search(text))
    rejected = _REVIEW_REJECTED_RE.search(text)
    needs = _NEEDS_RESEARCH_RE.search(text)
    feedback = rejected.group(1).strip() if rejected else ""
    question = needs.group(1).strip() if needs else ""
    cleaned = _ALL_VERDICT_RE.sub("", text).strip()
    return {
        "approved": approved,
        "needs_research": bool(needs),
        "feedback": feedback or question,
        "cleaned": cleaned,
    }


def review_round_block(rounds_completed: int) -> str:
    """Render the bounded-round context block for a Reviewer call.

    ``rounds_completed`` is the number of REJECTED review cycles so far
    (0-based). Total review rounds are MAX_REVIEW_RETRIES + 1; on the final
    round the Reviewer is explicitly required to approve.
    """
    current = rounds_completed + 1
    total = MAX_REVIEW_RETRIES + 1
    block = f"=== Review Round {current} of {total} ==="
    if current >= total:
        block += (
            "\nThis is the FINAL review round. You MUST end with "
            "[REVIEW_APPROVED] and present the best available version, "
            "fixing any remaining minor issues yourself."
        )
    return block
