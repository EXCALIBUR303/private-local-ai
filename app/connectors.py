"""
Application connectors.

Typed, purpose-built bridges to the apps on this Mac. The agent already has
raw AppleScript, so none of this adds *capability* — what it adds is
reliability: a model asked to "check my calendar" writes far better code by
calling `calendar_events(days=3)` than by inventing Calendar's AppleScript
dictionary from memory, and a named tool can carry an accurate risk level.

Everything here is local Apple software. Nothing touches a network service.

macOS gates most of this behind per-app Automation permission (System Settings
› Privacy & Security › Automation). The first call to each app raises the
system prompt; `permission_hint` turns the resulting error into something the
operator can act on.
"""
from __future__ import annotations

import json
import shlex
import subprocess
from dataclasses import dataclass
from typing import Any, Callable

from .power import PowerError, Risk, applescript

TIMEOUT = 45


#: AppleScript has no \x escape sequence: a literal "\x1f" in a script is a
#: syntax error, not a separator. The ASCII unit (31) and record (30)
#: separators exist for exactly this job and cannot occur in a subject line,
#: a track title or a file path, so they are built here and referenced as
#: `uSep` and `rSep` by every script below.
_PRELUDE = (
    "set uSep to (ASCII character 31)\n"
    "set rSep to (ASCII character 30)\n"
)


def _osa(script: str) -> str:
    result = applescript(_PRELUDE + script, timeout=TIMEOUT)
    if result["exit_code"] != 0:
        raise PowerError(permission_hint(result["stderr"]))
    return result["stdout"].strip()


def permission_hint(stderr: str) -> str:
    text = (stderr or "").strip()
    low = text.lower()
    if "not authorized" in low or "-1743" in low or "1743" in low:
        return (
            "macOS blocked this. Grant access in System Settings › Privacy & "
            "Security › Automation, then try again. " + text[:180]
        )
    if "application isn't running" in low or "-600" in low:
        return "That application is not running. Open it first. " + text[:160]
    return text[:300] or "The application refused the request."


def _records(raw: str) -> list[dict[str, str]]:
    """Parse the `field\x1funit\x1e` record stream the scripts below emit."""
    out = []
    for chunk in raw.split("\x1e"):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts = chunk.split("\x1f")
        out.append({str(i): p.strip() for i, p in enumerate(parts)})
    return out


# ───────────────────────────────── Mail ─────────────────────────────────
def mail_unread(limit: int = 10) -> dict[str, Any]:
    """Subjects and senders of unread mail. Reads only; sends nothing."""
    script = f'''
    tell application "Mail"
      set out to ""
      set msgs to (messages of inbox whose read status is false)
      set n to (count of msgs)
      if n > {limit} then set n to {limit}
      repeat with i from 1 to n
        set m to item i of msgs
        set out to out & (subject of m) & uSep & (sender of m) & ¬
          uSep & ((date received of m) as string) & rSep
      end repeat
      return out
    end tell'''
    rows = _records(_osa(script))
    return {"unread": [
        {"subject": r.get("0", ""), "from": r.get("1", ""), "received": r.get("2", "")}
        for r in rows
    ]}


def mail_draft(to: str, subject: str, body: str) -> dict[str, Any]:
    """
    Create a draft in Mail and show it. It is deliberately NOT sent — the
    operator presses send.
    """
    script = f'''
    tell application "Mail"
      set m to make new outgoing message with properties {{subject:{_q(subject)}, content:{_q(body)}, visible:true}}
      tell m to make new to recipient at end of to recipients with properties {{address:{_q(to)}}}
      activate
    end tell
    return "drafted"'''
    _osa(script)
    return {"drafted": True, "to": to, "subject": subject, "sent": False}


# ──────────────────────────────── Calendar ────────────────────────────────
def calendar_events(days: int = 7) -> dict[str, Any]:
    script = f'''
    tell application "Calendar"
      set out to ""
      set startDate to current date
      set endDate to startDate + ({int(days)} * days)
      repeat with c in calendars
        repeat with e in (events of c whose start date is greater than startDate ¬
                          and start date is less than endDate)
          set out to out & (summary of e) & uSep & ((start date of e) as string) & ¬
            uSep & (name of c) & rSep
        end repeat
      end repeat
      return out
    end tell'''
    rows = _records(_osa(script))
    return {"events": [
        {"title": r.get("0", ""), "start": r.get("1", ""), "calendar": r.get("2", "")}
        for r in rows
    ]}


def calendar_add(title: str, start: str, minutes: int = 60,
                 calendar: str | None = None) -> dict[str, Any]:
    """`start` is any string AppleScript's `date` understands, e.g. "3 March 2027 14:00"."""
    cal = f'calendar {_q(calendar)}' if calendar else "calendar 1"
    script = f'''
    tell application "Calendar"
      set s to date {_q(start)}
      tell {cal}
        make new event with properties {{summary:{_q(title)}, start date:s, end date:s + ({int(minutes)} * minutes)}}
      end tell
    end tell
    return "created"'''
    _osa(script)
    return {"created": title, "start": start, "minutes": minutes}


# ───────────────────────────────── Notes ─────────────────────────────────
def notes_create(title: str, body: str = "") -> dict[str, Any]:
    script = f'''
    tell application "Notes"
      tell default account
        make new note with properties {{name:{_q(title)}, body:{_q(body)}}}
      end tell
    end tell
    return "created"'''
    _osa(script)
    return {"created": title}


def notes_search(query: str, limit: int = 10) -> dict[str, Any]:
    script = f'''
    tell application "Notes"
      set out to ""
      set found to (notes whose name contains {_q(query)})
      set n to (count of found)
      if n > {limit} then set n to {limit}
      repeat with i from 1 to n
        set nt to item i of found
        set out to out & (name of nt) & uSep & ((modification date of nt) as string) & rSep
      end repeat
      return out
    end tell'''
    rows = _records(_osa(script))
    return {"notes": [{"title": r.get("0", ""), "modified": r.get("1", "")} for r in rows]}


# ──────────────────────────────── Reminders ────────────────────────────────
def reminders_add(text: str, due: str | None = None) -> dict[str, Any]:
    props = f'name:{_q(text)}'
    if due:
        props += f', due date:(date {_q(due)})'
    script = f'''
    tell application "Reminders"
      tell default list to make new reminder with properties {{{props}}}
    end tell
    return "created"'''
    _osa(script)
    return {"created": text, "due": due}


def reminders_list(limit: int = 20) -> dict[str, Any]:
    script = f'''
    tell application "Reminders"
      set out to ""
      set items to (reminders of default list whose completed is false)
      set n to (count of items)
      if n > {limit} then set n to {limit}
      repeat with i from 1 to n
        set r to item i of items
        set out to out & (name of r) & rSep
      end repeat
      return out
    end tell'''
    return {"reminders": [r.get("0", "") for r in _records(_osa(script))]}


# ───────────────────────────────── Music ─────────────────────────────────
def music_control(action: str = "playpause", query: str | None = None) -> dict[str, Any]:
    verbs = {"play": "play", "pause": "pause", "playpause": "playpause",
             "next": "next track", "previous": "previous track"}
    if action == "search" and query:
        script = f'''
        tell application "Music"
          set results to (every track whose name contains {_q(query)})
          set out to ""
          repeat with t in results
            set out to out & (name of t) & uSep & (artist of t) & rSep
          end repeat
          return out
        end tell'''
        rows = _records(_osa(script))
        return {"tracks": [{"name": r.get("0", ""), "artist": r.get("1", "")} for r in rows]}
    if action not in verbs:
        raise PowerError(f"Unknown music action. Use: {', '.join(verbs)} or search")
    _osa(f'tell application "Music" to {verbs[action]}')
    return {"music": action}


def now_playing() -> dict[str, Any]:
    script = '''
    tell application "Music"
      if player state is playing then
        return (name of current track) & uSep & (artist of current track)
      else
        return uSep & "not playing"
      end if
    end tell'''
    parts = _osa(script).split("\x1f")
    return {"track": parts[0] if parts else "", "artist": parts[1] if len(parts) > 1 else ""}


# ───────────────────────────────── Safari ─────────────────────────────────
def safari_tabs() -> dict[str, Any]:
    script = '''
    tell application "Safari"
      set out to ""
      repeat with w in windows
        repeat with t in tabs of w
          set out to out & (name of t) & uSep & (URL of t) & rSep
        end repeat
      end repeat
      return out
    end tell'''
    rows = _records(_osa(script))
    return {"tabs": [{"title": r.get("0", ""), "url": r.get("1", "")} for r in rows]}


def safari_open(url: str) -> dict[str, Any]:
    _osa(f'tell application "Safari" to open location {_q(url)}')
    return {"opened": url}


# ──────────────────────────────── Messages ────────────────────────────────
def messages_send(to: str, text: str) -> dict[str, Any]:
    """
    Send an iMessage. This one genuinely leaves the machine, so it is marked
    DESTRUCTIVE — it should stop for a human at anything below full autonomy.
    """
    script = f'''
    tell application "Messages"
      set svc to 1st account whose service type = iMessage
      send {_q(text)} to participant {_q(to)} of svc
    end tell
    return "sent"'''
    _osa(script)
    return {"sent_to": to, "characters": len(text)}


# ───────────────────────────────── Finder ─────────────────────────────────
def finder_selection() -> dict[str, Any]:
    script = '''
    tell application "Finder"
      set out to ""
      repeat with f in (get selection)
        set out to out & (POSIX path of (f as alias)) & rSep
      end repeat
      return out
    end tell'''
    return {"selected": [r.get("0", "") for r in _records(_osa(script))]}


def frontmost_app() -> dict[str, Any]:
    script = '''
    tell application "System Events"
      set p to first application process whose frontmost is true
      return name of p
    end tell'''
    return {"app": _osa(script)}


# ─────────────────────────────── Shortcuts ───────────────────────────────
def shortcuts_list(limit: int = 60) -> dict[str, Any]:
    """
    Every Shortcut on this Mac.

    This is the widest bridge in the app and the cheapest to maintain: whatever
    the operator has already automated in the Shortcuts editor — a HomeKit
    scene, a Things task, a Spotify playlist, a Slack message — becomes
    callable without this file knowing anything about those apps.
    """
    proc = subprocess.run(["/usr/bin/shortcuts", "list"],
                          capture_output=True, timeout=TIMEOUT, check=False)
    if proc.returncode != 0:
        raise PowerError(permission_hint(proc.stderr.decode("utf-8", "replace")))
    names = [n for n in proc.stdout.decode("utf-8", "replace").splitlines() if n.strip()]
    return {"shortcuts": names[:limit], "count": len(names)}


def shortcuts_run(name: str, input: str = "") -> dict[str, Any]:
    command = ["/usr/bin/shortcuts", "run", name]
    if input:
        command += ["--input-path", "-"]
    proc = subprocess.run(
        command, input=input.encode("utf-8") if input else None,
        capture_output=True, timeout=180, check=False,
    )
    if proc.returncode != 0:
        raise PowerError(
            permission_hint(proc.stderr.decode("utf-8", "replace"))
            or f"Shortcut {name!r} failed."
        )
    return {"shortcut": name,
            "output": proc.stdout.decode("utf-8", "replace")[:8000]}


# ──────────────────────────────── Contacts ────────────────────────────────
def contacts_find(query: str, limit: int = 10) -> dict[str, Any]:
    script = f'''
    tell application "Contacts"
      set out to ""
      set found to (people whose name contains {_q(query)})
      set n to (count of found)
      if n > {limit} then set n to {limit}
      repeat with i from 1 to n
        set p to item i of found
        set mail to ""
        if (count of emails of p) > 0 then set mail to (value of email 1 of p)
        set tel to ""
        if (count of phones of p) > 0 then set tel to (value of phone 1 of p)
        set out to out & (name of p) & uSep & mail & uSep & tel & rSep
      end repeat
      return out
    end tell'''
    rows = _records(_osa(script))
    return {"contacts": [
        {"name": r.get("0", ""), "email": r.get("1", ""), "phone": r.get("2", "")}
        for r in rows
    ]}


def _q(value: Any) -> str:
    """Quote a Python value as an AppleScript string literal."""
    text = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{text}"'


#: Connectors are grouped so the interface can say honestly where each one
#: reaches. `apple` and `system` never touch a network; `dev` does, and is
#: listed separately for exactly that reason.
CATEGORIES: dict[str, dict[str, str]] = {
    "apple": {
        "label": "Apple apps",
        "blurb": "Mail, Calendar, Notes, Reminders, Messages, Music. Local software on this Mac.",
        "reach": "local",
    },
    "system": {
        "label": "Mac & system",
        "blurb": "Finder, Safari, Shortcuts, the window server. Local software on this Mac.",
        "reach": "local",
    },
    "dev": {
        "label": "Developer services",
        "blurb": "Vercel, Railway, GitHub and friends. These reach the internet.",
        "reach": "network",
    },
}


@dataclass(frozen=True)
class Connector:
    name: str
    app: str
    risk: str
    summary: str
    params: dict[str, str]
    run: Callable[..., dict[str, Any]]
    category: str = "apple"


CONNECTORS: dict[str, Connector] = {c.name: c for c in [
    Connector("mail_unread", "Mail", Risk.READ, "List unread mail (subject, sender, date).",
              {}, mail_unread),
    Connector("mail_draft", "Mail", Risk.WRITE,
              "Open a pre-filled Mail draft. Never sends it.",
              {"to": "address", "subject": "subject", "body": "body"}, mail_draft),
    Connector("calendar_events", "Calendar", Risk.READ, "Upcoming events.",
              {"days": "how many days ahead"}, calendar_events),
    Connector("calendar_add", "Calendar", Risk.WRITE, "Create a calendar event.",
              {"title": "title", "start": "date string", "minutes": "duration"}, calendar_add),
    Connector("notes_create", "Notes", Risk.WRITE, "Create a note.",
              {"title": "title", "body": "body"}, notes_create),
    Connector("notes_search", "Notes", Risk.READ, "Find notes by title.",
              {"query": "text"}, notes_search),
    Connector("reminders_add", "Reminders", Risk.WRITE, "Add a real reminder.",
              {"text": "text", "due": "date string"}, reminders_add),
    Connector("reminders_list", "Reminders", Risk.READ, "List open reminders.",
              {}, reminders_list),
    Connector("music_control", "Music", Risk.WRITE,
              "Play, pause, skip, or search the music library.",
              {"action": "play|pause|playpause|next|previous|search", "query": "search text"},
              music_control),
    Connector("now_playing", "Music", Risk.READ, "What is playing.", {}, now_playing),
    Connector("safari_tabs", "Safari", Risk.READ, "List open Safari tabs.", {},
              safari_tabs, "system"),
    Connector("safari_open", "Safari", Risk.WRITE, "Open a URL in Safari.",
              {"url": "url"}, safari_open, "system"),
    Connector("messages_send", "Messages", Risk.DESTRUCTIVE,
              "Send an iMessage. This leaves the machine.",
              {"to": "phone or handle", "text": "message"}, messages_send),
    Connector("finder_selection", "Finder", Risk.READ, "Paths selected in Finder.",
              {}, finder_selection, "system"),
    Connector("frontmost_app", "System", Risk.READ, "Which app is in front.",
              {}, frontmost_app, "system"),
    Connector("shortcuts_list", "Shortcuts", Risk.READ,
              "List every Shortcut on this Mac.", {}, shortcuts_list, "system"),
    Connector("shortcuts_run", "Shortcuts", Risk.WRITE,
              "Run one of your Shortcuts by name — reaches any app you have "
              "already automated.",
              {"name": "shortcut name", "input": "text passed in (optional)"},
              shortcuts_run, "system"),
    Connector("contacts_find", "Contacts", Risk.READ,
              "Look someone up in Contacts.", {"query": "name"},
              contacts_find, "apple"),
]}


def catalog() -> dict[str, Any]:
    """Flat per-app listing. Kept for callers that only want the local bridges."""
    apps: dict[str, list[dict[str, Any]]] = {}
    for name, c in CONNECTORS.items():
        apps.setdefault(c.app, []).append(
            {"name": name, "risk": c.risk, "summary": c.summary,
             "params": c.params, "category": c.category}
        )
    return apps


def grouped() -> list[dict[str, Any]]:
    """
    The catalogue the interface renders: categories, each holding its apps.

    Developer services are appended as their own category rather than mixed in,
    because the distinction that matters to the operator is not "which app" but
    "does this leave my Mac".
    """
    from . import devservices

    out = []
    for key, meta in CATEGORIES.items():
        if key == "dev":
            continue
        apps: dict[str, list[dict[str, Any]]] = {}
        for name, c in CONNECTORS.items():
            if c.category != key:
                continue
            apps.setdefault(c.app, []).append(
                {"name": name, "risk": c.risk, "summary": c.summary,
                 "params": c.params}
            )
        out.append({
            "id": key, **meta,
            "apps": [{"app": app, "tools": tools} for app, tools in sorted(apps.items())],
        })

    out.append({
        "id": "dev", **CATEGORIES["dev"],
        "apps": [
            {"app": p["label"], "service": p, "tools": [
                {"name": f"watch:{p['id']}", "risk": Risk.READ,
                 "summary": p["blurb"], "params": p["fields"]}
            ]}
            for p in devservices.catalog()
        ],
    })
    return out
