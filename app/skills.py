"""
Skills — user-defined capabilities, in the spirit of Claude's own.

A skill is a single Markdown file with a small YAML-ish header. Two kinds:

  * **Instruction skills** carry only guidance. When one matches, its body is
    injected into the agent's context so the model follows your procedure
    instead of improvising one.
  * **Command skills** additionally carry a `run:` block — a shell script the
    agent can execute as a single named tool, with `{{placeholders}}` filled
    from arguments. This turns a recipe you trust into one reliable call.

Skills live in `<data>/skills/*.md` so they are yours: editable in any text
editor, diffable, and portable between machines.

They inherit the same safety posture as everything else — a command skill is
classified by blast radius and stops for approval below `full` autonomy.
"""
from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import config
from .power import Risk, PowerError, classify_shell, run_shell

SKILLS_DIR = config.DATA_DIR / "skills"
_HEADER = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.S)
_PLACEHOLDER = re.compile(r"\{\{\s*(\w+)\s*\}\}")
_NAME_OK = re.compile(r"^[a-z0-9][a-z0-9_-]{1,40}$")


@dataclass
class Skill:
    name: str
    description: str
    when: str
    body: str
    command: str | None
    path: Path

    @property
    def kind(self) -> str:
        return "command" if self.command else "instruction"

    @property
    def risk(self) -> str:
        return classify_shell(self.command) if self.command else Risk.READ

    @property
    def params(self) -> list[str]:
        source = self.command or ""
        seen, out = set(), []
        for m in _PLACEHOLDER.finditer(source):
            if m.group(1) not in seen:
                seen.add(m.group(1))
                out.append(m.group(1))
        return out

    def render(self, args: dict[str, Any]) -> str:
        if not self.command:
            raise PowerError(f"Skill {self.name!r} has no command to run.")
        missing = [p for p in self.params if p not in args]
        if missing:
            raise PowerError(f"Skill {self.name!r} needs: {', '.join(missing)}")

        def sub(m: re.Match) -> str:
            # Values are quoted so a skill argument can never break out of the
            # command it is substituted into.
            import shlex
            return shlex.quote(str(args[m.group(1)]))

        return _PLACEHOLDER.sub(sub, self.command)


def ensure_dir() -> None:
    SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    if not any(SKILLS_DIR.glob("*.md")):
        _write_examples()


def _parse(path: Path) -> Skill | None:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    m = _HEADER.match(raw)
    if not m:
        return None

    meta: dict[str, str] = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            meta[key.strip().lower()] = value.strip().strip('"').strip("'")

    body = m.group(2).strip()
    command = None
    fence = re.search(r"```(?:bash|sh|zsh)?\s*\n(.*?)```", body, re.S)
    if meta.get("run", "").lower() in ("shell", "bash", "zsh") and fence:
        command = fence.group(1).strip()
        body = body[: fence.start()].strip()

    name = meta.get("name") or path.stem
    name = name.lower().replace(" ", "-")
    if not _NAME_OK.match(name):
        return None
    return Skill(
        name=name,
        description=meta.get("description", ""),
        when=meta.get("when", ""),
        body=body,
        command=command,
        path=path,
    )


def load_all() -> dict[str, Skill]:
    ensure_dir()
    out: dict[str, Skill] = {}
    for path in sorted(SKILLS_DIR.glob("*.md")):
        skill = _parse(path)
        if skill:
            out[skill.name] = skill
    return out


def get(name: str) -> Skill | None:
    return load_all().get((name or "").lower())


def run(name: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
    skill = get(name)
    if skill is None:
        raise PowerError(f"No skill named {name!r}.")
    if not skill.command:
        return {"skill": skill.name, "kind": "instruction", "guidance": skill.body}
    command = skill.render(args or {})
    result = run_shell(command)
    return {"skill": skill.name, "command": command, **result}


def catalog() -> dict[str, Any]:
    return {
        name: {
            "description": s.description,
            "when": s.when,
            "kind": s.kind,
            "risk": s.risk,
            "params": s.params,
            "file": s.path.name,
            "body": s.body[:600],
        }
        for name, s in load_all().items()
    }


def save(name: str, text: str) -> dict[str, Any]:
    """Write a skill file. `text` is the whole Markdown document."""
    ensure_dir()
    slug = (name or "").lower().replace(" ", "-")
    if not _NAME_OK.match(slug):
        raise PowerError(
            "Skill names are lowercase letters, digits, - and _ (2-41 chars)."
        )
    path = SKILLS_DIR / f"{slug}.md"
    path.write_text(text, encoding="utf-8")
    if _parse(path) is None:
        path.unlink(missing_ok=True)
        raise PowerError(
            "That skill could not be parsed. It needs a --- header with at "
            "least a name and description."
        )
    return {"saved": slug, "file": str(path)}


def delete(name: str) -> bool:
    skill = get(name)
    if skill is None:
        return False
    skill.path.unlink(missing_ok=True)
    return True


def prompt_block() -> str:
    """A short index of skills, for the agent's system prompt."""
    skills = load_all()
    if not skills:
        return ""
    lines = []
    for s in skills.values():
        hint = f" — use {s.when}" if s.when else ""
        lines.append(f"- {s.name}: {s.description}{hint}")
    return (
        "Skills you have been given (prefer these over improvising a "
        "procedure):\n" + "\n".join(lines)
    )


def register_tools(tools: dict[str, Any], tool_cls: Any) -> None:
    """Expose every command skill as its own agent tool."""
    for name, skill in load_all().items():
        if not skill.command:
            continue
        tool_name = f"skill_{name.replace('-', '_')}"
        params = {p: f"value for {p}" for p in skill.params}
        tools[tool_name] = tool_cls(
            tool_name,
            skill.risk,
            f"[skill] {skill.description or skill.name}",
            params,
            (lambda n: lambda **kw: run(n, kw))(name),
        )


def _write_examples() -> None:
    """Ship a couple of readable examples so the format is obvious."""
    (SKILLS_DIR / "disk-hogs.md").write_text(
        """---
name: disk-hogs
description: Find the biggest files in a folder
when: asked what is taking up space
run: shell
---
Lists the twenty largest files under a folder, biggest first.

```bash
find {{folder}} -type f -exec du -h {} + 2>/dev/null | sort -rh | head -20
```
""",
        encoding="utf-8",
    )
    (SKILLS_DIR / "code-review.md").write_text(
        """---
name: code-review
description: How to review code in this house style
when: asked to review a diff or a file
---
Review in this order, and stop at the first category that finds something real:

1. Correctness — does it do what it claims, including edge cases?
2. Failure modes — what happens on empty input, a network error, a full disk?
3. Clarity — will the next reader understand it without asking?
4. Tests — is the risky part actually covered?

Report findings most severe first. Quote the exact line. If nothing is wrong,
say so plainly rather than inventing nits.
""",
        encoding="utf-8",
    )


def reset_examples() -> dict[str, Any]:
    if SKILLS_DIR.exists():
        shutil.rmtree(SKILLS_DIR, ignore_errors=True)
    ensure_dir()
    return {"reset": True, "count": len(load_all())}
