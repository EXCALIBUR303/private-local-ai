"""
Unrestricted Mac control.

The operator explicitly asked for shell, filesystem, AppleScript, browser and
application control, overriding the original four-action allowlist. This module
provides it, honestly and without secret crippling.

What is NOT removed, because it is the only thing standing between a poisoned
document and arbitrary code execution:

  * Autonomy is a switch the operator owns. At `off` every action needs a
    click. At `safe` only non-mutating actions run unattended. At `full`
    everything runs.
  * Every action is classified by blast radius, so `safe` means something.
  * Everything is logged, and a kill switch stops an agent mid-run.

The threat is real and specific: this app ingests untrusted PDFs, and a
passage saying "run this command" reaches the model like any other text. The
switch is what keeps that from being decisive.
"""
from __future__ import annotations

import base64
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import config

SHELL = "/bin/zsh"
DEFAULT_TIMEOUT = 60


class PowerError(RuntimeError):
    """Execution failed. The message is safe to show the operator."""


# --------------------------------------------------------------- taxonomy
class Risk:
    READ = "read"        # observes only
    WRITE = "write"      # changes files or app state
    DESTRUCTIVE = "destructive"  # deletes, overwrites, or is irreversible
    SYSTEM = "system"    # power, network, security surfaces


@dataclass(frozen=True)
class Tool:
    name: str
    risk: str
    summary: str
    params: dict[str, str]
    run: Callable[..., dict[str, Any]]


#: Commands that are effectively irreversible. Even with autonomy at `safe`
#: these require a human; at `full` they run like anything else.
_DESTRUCTIVE_SHELL = re.compile(
    r"\brm\s+(-[a-z]*[rf]|--recursive|--force)"
    r"|\brm\s+-\w*f|\bmkfs\b|\bdd\s+if=|\bshutdown\b|\breboot\b|\bhalt\b"
    r"|\bdiskutil\s+(erase|reformat|partition)"
    r"|\bkillall\s+-9\b|\bchmod\s+-R\s+000|\b:\s*\(\)\s*\{.*\};:"
    r"|>\s*/dev/(disk|rdisk)|\bsudo\s+rm\b"
    r"|\bgit\s+(push\s+--force|reset\s+--hard)"
    r"|\bcurl\b[^|]*\|\s*(sudo\s+)?(ba)?sh"
    r"|\bwget\b[^|]*\|\s*(sudo\s+)?(ba)?sh",
    re.I,
)

_READONLY_SHELL = re.compile(
    r"^\s*(ls|cat|head|tail|grep|rg|find|wc|du|df|ps|top|whoami|pwd|date|uname|"
    r"echo|which|type|env|printenv|stat|file|man|git\s+(status|log|diff|show|branch)|"
    r"brew\s+(list|info)|pip\s+list|npm\s+ls|sw_vers|system_profiler|uptime|"
    r"sysctl|networksetup\s+-listall|scutil\s+--get)\b",
    re.I,
)


#: Words a model uses to mean "no". The Ollama tool schema declares every
#: parameter as a string, so a model that correctly says `append: "false"` sends
#: the four characters f-a-l-s-e — which is a non-empty string, and therefore
#: true. Left uncoerced this silently inverts the caller's intent: a model
#: asking to overwrite a file appends to it instead, and `risk_of` grades the
#: call as an ordinary write rather than the destructive overwrite it is.
_FALSEY = {"false", "0", "no", "none", "null", "off", "", "f", "n"}


def as_bool(value: Any) -> bool:
    """Interpret a tool argument as a boolean, string spellings included."""
    if isinstance(value, str):
        return value.strip().lower() not in _FALSEY
    return bool(value)


def as_int(value: Any, default: int = 0) -> int:
    """Interpret a tool argument as an integer. Models send `"23"`, not `23`."""
    if isinstance(value, bool):
        return int(value)
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def classify_shell(command: str) -> str:
    """Best-effort blast-radius estimate for a shell command."""
    if _DESTRUCTIVE_SHELL.search(command):
        return Risk.DESTRUCTIVE
    # A pipeline or redirect can hide a write behind a read-looking head.
    if _READONLY_SHELL.match(command) and not re.search(r"[>|;&]|\$\(", command):
        return Risk.READ
    return Risk.WRITE


# ------------------------------------------------------------------ shell
def _sh(command: str, timeout: int = DEFAULT_TIMEOUT, cwd: str | None = None) -> dict[str, Any]:
    """
    Run a command through a login shell.

    A login shell is deliberate: the operator expects their PATH, aliases and
    tool managers (brew, nvm, pyenv) to be present, the same as in Terminal.
    """
    work = os.path.expanduser(cwd) if cwd else str(Path.home())
    try:
        proc = subprocess.run(
            [SHELL, "-lc", command],
            capture_output=True, timeout=timeout, cwd=work, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise PowerError(f"Timed out after {timeout}s.") from exc
    except OSError as exc:
        raise PowerError(str(exc)) from exc

    out = proc.stdout.decode("utf-8", "replace")
    err = proc.stderr.decode("utf-8", "replace")
    limit = 20000
    return {
        "exit_code": proc.returncode,
        "stdout": out[:limit] + ("\n…truncated…" if len(out) > limit else ""),
        "stderr": err[:limit] + ("\n…truncated…" if len(err) > limit else ""),
        "cwd": work,
    }


def run_shell(command: str, cwd: str | None = None, timeout: int = DEFAULT_TIMEOUT) -> dict[str, Any]:
    if not isinstance(command, str) or not command.strip():
        raise PowerError("A command is required.")
    return _sh(command, timeout=timeout, cwd=cwd)


def applescript(script: str, timeout: int = DEFAULT_TIMEOUT) -> dict[str, Any]:
    if not isinstance(script, str) or not script.strip():
        raise PowerError("A script is required.")
    try:
        proc = subprocess.run(
            ["/usr/bin/osascript", "-e", script],
            capture_output=True, timeout=timeout, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        # The overwhelmingly common cause is macOS holding the script while its
        # Automation permission dialog waits for a click — the first call to
        # each app does this. Saying "timed out" alone sends people hunting for
        # a bug that is actually a dialog behind the window.
        raise PowerError(
            f"AppleScript timed out after {timeout}s. If this was the first "
            "time reaching that app, macOS is probably showing an Automation "
            "permission dialog — allow it and try again. Otherwise the app may "
            "be busy or showing a dialog of its own."
        ) from exc
    return {
        "exit_code": proc.returncode,
        "stdout": proc.stdout.decode("utf-8", "replace")[:20000],
        "stderr": proc.stderr.decode("utf-8", "replace")[:4000],
    }


# ------------------------------------------------------------- filesystem
def read_file(path: str, max_bytes: int = 400_000) -> dict[str, Any]:
    p = Path(os.path.expanduser(path))
    if not p.exists():
        raise PowerError(f"No such file: {p}")
    if p.is_dir():
        raise PowerError(f"{p} is a directory — use list_dir.")
    data = p.read_bytes()[:max_bytes]
    try:
        text = data.decode("utf-8")
        return {"path": str(p), "bytes": p.stat().st_size, "text": text}
    except UnicodeDecodeError:
        return {"path": str(p), "bytes": p.stat().st_size,
                "base64": base64.b64encode(data[:60_000]).decode(),
                "note": "binary file, base64 of the first 60 KB"}


def write_file(path: str, content: str, append: Any = False) -> dict[str, Any]:
    p = Path(os.path.expanduser(path))
    p.parent.mkdir(parents=True, exist_ok=True)
    existed = p.exists()
    append = as_bool(append)
    with p.open("a" if append else "w", encoding="utf-8") as fh:
        fh.write(content)
    return {"path": str(p), "bytes": p.stat().st_size,
            "action": "appended" if append else ("overwrote" if existed else "created")}


def list_dir(path: str = "~", limit: int = 400) -> dict[str, Any]:
    p = Path(os.path.expanduser(path))
    if not p.is_dir():
        raise PowerError(f"Not a directory: {p}")
    entries = []
    for child in sorted(p.iterdir())[:limit]:
        try:
            st = child.stat()
            entries.append({"name": child.name, "dir": child.is_dir(), "bytes": st.st_size})
        except OSError:
            continue
    return {"path": str(p), "count": len(entries), "entries": entries}


def move_to_trash(path: str) -> dict[str, Any]:
    """Delete via the Finder's Trash, so it stays recoverable."""
    p = Path(os.path.expanduser(path))
    if not p.exists():
        raise PowerError(f"No such path: {p}")
    script = f'tell application "Finder" to move POSIX file "{p}" to trash'
    result = applescript(script)
    if result["exit_code"] != 0:
        raise PowerError(result["stderr"] or "Finder refused to trash that item.")
    return {"trashed": str(p)}


# ------------------------------------------------------------ app control
def control_app(app: str, action: str = "activate") -> dict[str, Any]:
    verbs = {"activate": "activate", "quit": "quit", "hide": "set visible to false"}
    if action not in verbs:
        raise PowerError(f"Unknown app action '{action}'. Use: {', '.join(verbs)}")
    safe_app = app.replace('"', '')
    result = applescript(f'tell application "{safe_app}" to {verbs[action]}')
    if result["exit_code"] != 0:
        raise PowerError(result["stderr"] or f"Could not {action} {app}.")
    return {"app": safe_app, "action": action}


def open_path_or_url(target: str) -> dict[str, Any]:
    subprocess.run([config.OPEN_BIN, "--", target], check=False, timeout=20)
    return {"opened": target}


def clipboard_read() -> dict[str, Any]:
    proc = subprocess.run(["/usr/bin/pbpaste"], capture_output=True, timeout=10, check=False)
    return {"text": proc.stdout.decode("utf-8", "replace")[:100_000]}


def clipboard_write(text: str) -> dict[str, Any]:
    proc = subprocess.run(["/usr/bin/pbcopy"], input=text.encode("utf-8"),
                          timeout=10, check=False)
    return {"copied": len(text), "ok": proc.returncode == 0}


def screenshot(path: str | None = None) -> dict[str, Any]:
    config.ensure_dirs()
    dest = Path(os.path.expanduser(path)) if path else config.TMP_DIR / "screen.png"
    proc = subprocess.run(["/usr/sbin/screencapture", "-x", str(dest)],
                          capture_output=True, timeout=30, check=False)
    if proc.returncode != 0 or not dest.exists():
        raise PowerError(
            "screencapture failed — macOS Screen Recording permission is likely "
            "not granted to this app (System Settings › Privacy & Security)."
        )
    return {"path": str(dest), "bytes": dest.stat().st_size}


def notify(title: str, message: str = "") -> dict[str, Any]:
    t = title.replace('"', "'")
    m = message.replace('"', "'")
    applescript(f'display notification "{m}" with title "{t}"')
    return {"notified": title}


def system_info() -> dict[str, Any]:
    return _sh("sw_vers; echo; uptime; echo; df -h / | tail -1")


# ---------------------------------------------------------------- registry
TOOLS: dict[str, Tool] = {
    t.name: t for t in [
        Tool("run_shell", Risk.WRITE,
             "Run any shell command in a login shell.",
             {"command": "the command", "cwd": "working directory (optional)"},
             run_shell),
        Tool("applescript", Risk.WRITE,
             "Run arbitrary AppleScript (Mail, Calendar, Notes, Finder, any app).",
             {"script": "the AppleScript source"}, applescript),
        Tool("read_file", Risk.READ,
             "Read any file on the disk.", {"path": "absolute or ~ path"}, read_file),
        Tool("write_file", Risk.WRITE,
             "Write or append to any file.",
             {"path": "path", "content": "text", "append": "true/false"}, write_file),
        Tool("list_dir", Risk.READ,
             "List any directory.", {"path": "path"}, list_dir),
        Tool("move_to_trash", Risk.DESTRUCTIVE,
             "Move a file or folder to the Trash.", {"path": "path"}, move_to_trash),
        Tool("control_app", Risk.WRITE,
             "Activate, hide or quit an application.",
             {"app": "application name", "action": "activate|hide|quit"}, control_app),
        Tool("open_path_or_url", Risk.WRITE,
             "Open a file, folder or URL in its default handler.",
             {"target": "path or URL"}, open_path_or_url),
        Tool("clipboard_read", Risk.READ, "Read the clipboard.", {}, clipboard_read),
        Tool("clipboard_write", Risk.WRITE, "Replace the clipboard contents.",
             {"text": "text"}, clipboard_write),
        Tool("screenshot", Risk.READ, "Capture the screen to a file.",
             {"path": "destination (optional)"}, screenshot),
        Tool("notify", Risk.WRITE, "Post a macOS notification.",
             {"title": "title", "message": "body"}, notify),
        Tool("system_info", Risk.READ, "OS version, uptime and free disk.", {},
             system_info),
    ]
}


def register_connectors() -> None:
    """
    Fold the per-app connectors into the tool registry.

    Imported lazily to keep `power` free of a circular dependency: connectors
    builds on `applescript` and `Risk` from this module.
    """
    from . import connectors

    for name, c in connectors.CONNECTORS.items():
        TOOLS[name] = Tool(name, c.risk, f"[{c.app}] {c.summary}", c.params, c.run)

    # the coding workbench: precise edits, project search, test running
    from . import coder

    coder.register_tools(TOOLS, Tool)

    # user-authored skills become named tools of their own
    from . import skills as skills_mod

    skills_mod.register_tools(TOOLS, Tool)


def risk_of(name: str, params: dict[str, Any]) -> str:
    """Blast radius for a specific invocation."""
    tool = TOOLS.get(name)
    if tool is None:
        raise PowerError(f"Unknown tool: {name}")
    if name == "run_shell":
        return classify_shell(str(params.get("command", "")))
    if name == "write_file" and not as_bool(params.get("append")):
        target = Path(os.path.expanduser(str(params.get("path", ""))))
        if target.exists():
            return Risk.DESTRUCTIVE           # overwriting loses the original
    return tool.risk


def execute(name: str, params: dict[str, Any]) -> dict[str, Any]:
    tool = TOOLS.get(name)
    if tool is None:
        raise PowerError(f"Unknown tool: {name}")
    clean = {k: v for k, v in (params or {}).items() if k in tool.params or not tool.params}
    try:
        return tool.run(**clean)
    except TypeError as exc:
        raise PowerError(f"Bad parameters for {name}: {exc}") from exc


def catalog() -> dict[str, Any]:
    return {
        name: {"risk": t.risk, "summary": t.summary, "params": t.params}
        for name, t in TOOLS.items()
    }


def ollama_tool_schema(names: list[str] | None = None) -> list[dict[str, Any]]:
    """
    Tool definitions in the shape Ollama's chat API expects.

    `names` narrows the offer. Handing a 7B model all 38 tools when the task is
    a code change means it picks its way past `music_control` and
    `messages_send` on the way to `patch_file`; a short, relevant list measurably
    improves which tool it reaches for.
    """
    out = []
    wanted = set(names) if names else None
    for name, t in TOOLS.items():
        if wanted is not None and name not in wanted:
            continue
        props = {k: {"type": "string", "description": v} for k, v in t.params.items()}
        out.append({
            "type": "function",
            "function": {
                "name": name,
                "description": f"{t.summary} (risk: {t.risk})",
                "parameters": {"type": "object", "properties": props,
                               "required": list(props)[:1]},
            },
        })
    return out
