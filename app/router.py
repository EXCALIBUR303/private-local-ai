"""
Task routing.

Different engineering work wants a different model *and* a different way of
running it. The second half matters as much as the first: the same weights at
`think=True, temperature=0.7, num_ctx=8192` behave nothing like the same
weights at `think=False, temperature=0.1, num_ctx=4096`. A profile therefore
carries both.

Classification is a deterministic heuristic, not another model call. On a
16 GB machine an extra round trip to decide which model to use would cost more
than it saves, and a rule you can read is easier to trust than a hidden one.

Memory reality: this Mac holds one 7-8B model comfortably. Switching category
between two large models forces Ollama to unload and reload (a few seconds), so
the profiles below deliberately reuse `qwen3:8b` wherever a specialist would
not clearly win, and only divert to the coder or the small model when the task
genuinely differs.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from . import config


@dataclass(frozen=True)
class Profile:
    """A category, the weights it runs on, and how to run them."""
    key: str
    label: str
    why: str
    model: str
    temperature: float
    num_ctx: int
    think: bool = False
    fallbacks: tuple[str, ...] = field(default_factory=tuple)


#: Ordered by specificity — the first match wins.
PROFILES: dict[str, Profile] = {
    "code": Profile(
        key="code",
        label="Code",
        why="Code-specialised weights; near-zero temperature so syntax is not improvised.",
        model="qwen2.5-coder:7b",
        temperature=0.1,
        num_ctx=8192,
        think=False,
        fallbacks=("qwen3:8b",),
    ),
    "reasoning": Profile(
        key="reasoning",
        label="Reasoning",
        why="Deliberate multi-step thinking is switched on for design and analysis.",
        model="qwen3:8b",
        temperature=0.6,
        num_ctx=8192,
        think=True,
        fallbacks=("phi4-mini",),
    ),
    "documents": Profile(
        key="documents",
        label="Documents",
        why="Low temperature and a wider window so answers stay faithful to the passages.",
        model="qwen3:8b",
        temperature=0.2,
        num_ctx=8192,
        think=False,
        fallbacks=("phi4-mini",),
    ),
    "quick": Profile(
        key="quick",
        label="Quick",
        why="A small model answers short factual questions in well under a second.",
        model="phi4-mini",
        temperature=0.3,
        num_ctx=4096,
        think=False,
        fallbacks=("qwen3:8b",),
    ),
    "general": Profile(
        key="general",
        label="General",
        why="The balanced default for open-ended conversation.",
        model="qwen3:8b",
        temperature=0.6,
        num_ctx=config.NUM_CTX,
        think=False,
        fallbacks=("phi4-mini",),
    ),
}

# ---------------------------------------------------------------- signals
_FENCE = re.compile(r"```|~~~")
_TRACEBACK = re.compile(
    r"traceback \(most recent call last\)|^\s*at [\w.$]+\(|SyntaxError|TypeError:"
    r"|NullPointerException|panic:|segmentation fault|error\[E\d+\]",
    re.I | re.M,
)
_PATH_OR_EXT = re.compile(
    r"\b[\w./-]+\.(py|js|ts|tsx|jsx|go|rs|java|kt|swift|rb|php|c|h|cpp|hpp|cs|sh|"
    r"sql|yaml|yml|toml|json|tf|dockerfile)\b",
    re.I,
)
_CODE_WORDS = re.compile(
    r"\b(refactor|debug|stack ?trace|compile|compiler|linter|unit test|regex|"
    r"function|method|class|variable|async|await|promise|pointer|null pointer|"
    r"segfault|dependency|package\.json|requirements\.txt|docker|kubernetes|k8s|"
    r"sql query|join|index|migration|api endpoint|http handler|middleware|"
    r"typescript|javascript|python|golang|rust|java|kotlin|swift|c\+\+|bash script|"
    r"git rebase|merge conflict|pull request|code review|implement|write a script|"
    r"snippet|syntax|exception|traceback|runtime error|memory leak)\b",
    re.I,
)
_REASON_WORDS = re.compile(
    r"\b(why|trade[- ]?off|trade[- ]?offs|architecture|architect|design (a|the|an)|"
    r"compare|versus|vs\.?|pros and cons|should (i|we)|which (is|would|one)|"
    r"prove|derive|calculate|complexity|big[- ]?o|scal(e|ing|ability)|"
    r"bottleneck|root cause|reason(ing)?|analy[sz]e|evaluate|strategy|"
    r"plan (a|the|out)|step by step|think through|justify|explain how)\b",
    re.I,
)
_QUICK_WORDS = re.compile(
    r"^(what|who|when|where|define|meaning of|abbreviation|stands for|"
    r"how do i spell|convert|translate)\b",
    re.I,
)

CATEGORY_ORDER = ("code", "reasoning", "quick", "general")


@dataclass
class Decision:
    category: str
    profile: Profile
    model: str
    reason: str
    auto: bool


def classify(text: str, use_documents: bool = False) -> tuple[str, str]:
    """
    Return (category, human-readable reason).

    Document questions are routed by intent, not content: if the operator has
    asked to use their files, faithfulness beats everything else.
    """
    t = (text or "").strip()
    if use_documents:
        return "documents", "answering from your indexed documents"
    if not t:
        return "general", "empty prompt"

    lowered = t.lower()
    words = len(t.split())

    if _FENCE.search(t):
        return "code", "the prompt contains a code block"
    if _TRACEBACK.search(t):
        return "code", "the prompt contains an error trace"
    if _PATH_OR_EXT.search(t):
        return "code", "the prompt names a source file"

    code_hits = len(_CODE_WORDS.findall(lowered))
    reason_hits = len(_REASON_WORDS.findall(lowered))

    # A design question about code is still a design question.
    if code_hits and reason_hits and reason_hits >= code_hits:
        return "reasoning", "a design question about engineering work"
    if code_hits:
        return "code", "programming vocabulary in the prompt"
    if reason_hits:
        return "reasoning", "the prompt asks for analysis or a judgement"
    if words <= 12 and _QUICK_WORDS.search(lowered):
        return "quick", "a short factual question"
    if words <= 6:
        return "quick", "a very short prompt"
    return "general", "open-ended conversation"


def available_model(profile: Profile, installed: list[str]) -> tuple[str, str | None]:
    """
    Pick the profile's model, or the first fallback that is actually on disk.
    Returns (model, note) where note explains any substitution.
    """
    def present(name: str) -> bool:
        return name in installed or f"{name}:latest" in installed

    if present(profile.model):
        return profile.model, None
    for alt in profile.fallbacks:
        if present(alt):
            return alt, (
                f"{profile.model} is not installed; using {alt}. "
                f"Run: ollama pull {profile.model}"
            )
    if installed:
        return config.CHAT_MODEL, f"{profile.model} is not installed."
    return profile.model, None


def route(
    text: str,
    use_documents: bool,
    installed: list[str],
    override: str | None = None,
) -> Decision:
    """
    Decide how to answer.

    `override` is a category name (manual routing) or None for automatic.
    """
    if override and override in PROFILES:
        profile = PROFILES[override]
        model, note = available_model(profile, installed)
        reason = "you selected this category" + (f" — {note}" if note else "")
        return Decision(override, profile, model, reason, auto=False)

    category, why = classify(text, use_documents)
    profile = PROFILES[category]
    model, note = available_model(profile, installed)
    return Decision(category, profile, model,
                    why + (f" — {note}" if note else ""), auto=True)


def catalog() -> dict[str, Any]:
    from . import naming

    return {
        "order": list(CATEGORY_ORDER) + ["documents"],
        "profiles": {
            k: {
                "label": p.label,
                "friendly": naming.friendly(p.model),
                "why": p.why,
                "model": p.model,
                "temperature": p.temperature,
                "num_ctx": p.num_ctx,
                "think": p.think,
                "fallbacks": list(p.fallbacks),
            }
            for k, p in PROFILES.items()
        },
    }
