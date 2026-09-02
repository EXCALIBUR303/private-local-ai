"""
Friendly model names.

`qwen2.5-coder:7b` tells you who trained it; `Forge` tells you what it is for.
The registry IDs stay authoritative — they are what Ollama loads, and they are
still shown as the subtitle everywhere — but the interface leads with a short
name you can actually remember and say out loud.

Names are chosen by role, not by vendor, so swapping the weights behind a role
does not change what you call it.
"""
from __future__ import annotations

from typing import Any

#: role name -> (model id, one-line job, subsystem hue)
NAMES: dict[str, dict[str, str]] = {
    "Core": {
        "model": "qwen3:8b",
        "job": "Main brain. General conversation, reasoning and documents.",
        "hue": "amber",
    },
    "Dash": {
        "model": "phi4-mini",
        "job": "Fast answers. Short questions, under a second.",
        "hue": "cyan",
    },
    "Forge": {
        "model": "qwen2.5-coder:7b",
        "job": "Code. Writing, debugging and refactoring.",
        "hue": "violet",
    },
    "Index": {
        "model": "nomic-embed-text",
        "job": "Reads your documents into searchable meaning.",
        "hue": "mint",
    },
    "Ear": {
        "model": "whisper base.en",
        "job": "Turns your speech into text.",
        "hue": "coral",
    },
    "Voice": {
        "model": "en_GB-alan / cloned",
        "job": "Speaks replies aloud.",
        "hue": "coral",
    },
}

#: Work modes. `lanes` is how many agent tasks the mode runs at once.
MODES: dict[str, dict[str, Any]] = {
    "Dash": {
        "model": "phi4-mini",
        "lanes": 1,
        "blurb": "Everyday light work — quick questions, small edits, one thing at a time.",
        "detail": "3.8B, ~2.6 GB resident. Answers in well under a second.",
        "hue": "cyan",
    },
    "Core": {
        "model": "qwen3:8b",
        "lanes": 1,
        "blurb": "The balanced default for conversation and documents.",
        "detail": "8.2B, ~5.3 GB resident. ~22 tokens/s on this Mac.",
        "hue": "amber",
    },
    "Forge": {
        "model": "qwen2.5-coder:7b",
        "lanes": 1,
        "blurb": "Code — writing, debugging and refactoring.",
        "detail": "7B code-specialised, ~4.7 GB resident, near-zero temperature.",
        "hue": "violet",
    },
    "Atlas": {
        "model": "qwen3:8b",
        "lanes": 3,
        "blurb": "Heavy work — runs up to three tasks at once and thinks them through.",
        "detail": (
            "Core's weights with deliberate reasoning on and three parallel "
            "lanes. One model stays loaded; the lanes share it, which is what "
            "keeps this inside 16 GB."
        ),
        "hue": "mint",
    },
}


def mode(name: str | None) -> dict[str, Any]:
    return MODES.get(name or "Core", MODES["Core"])


def modes() -> dict[str, Any]:
    return {k: {**v, "name": k} for k, v in MODES.items()}


_BY_MODEL = {v["model"]: k for k, v in NAMES.items()}


def friendly(model_id: str | None) -> str:
    """'qwen3:8b' -> 'Core'. Unknown models keep their own id."""
    if not model_id:
        return "—"
    if model_id in _BY_MODEL:
        return _BY_MODEL[model_id]
    base = model_id.replace(":latest", "")
    return _BY_MODEL.get(base, base)


def model_for(name: str) -> str | None:
    """'Core' -> 'qwen3:8b'. Case-insensitive."""
    for key, value in NAMES.items():
        if key.lower() == (name or "").lower():
            return value["model"]
    return None


def catalog() -> dict[str, Any]:
    return {
        name: {**meta, "friendly": name}
        for name, meta in NAMES.items()
    }
