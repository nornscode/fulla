"""The `mac_*` tools. Each is a thin shell over AppleScript, the Messages
database or tmux, guarded by what the owner allowed in config: which apps
AppleScript may address, who iMessage may be sent to, which tmux sessions
may be read and typed into, and which Shortcuts folder holds the home
automations. Nothing here knows about Norns beyond the `@tool` decorator.

An iMessage goes only as a draft the owner approved in the chat: the one
sending tool takes a draft id and digest, claims the approved text from Fly
(confirm.claim), and sends exactly that, once. Messages is never scriptable
through `mac_applescript`, so there is no other way to send.
"""

# Module state is set by init() before any tool runs; it is None only before then.
# pyright: reportOptionalMemberAccess=false

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

from fulla import confirm, shell, tmux, zones
from fulla.config import MacConfig
from fulla.mac import osa
from fulla.tooldef import tool
from fulla.untrusted import untrusted

MAX_OUTPUT = 4000
CHAT_DB = Path.home() / "Library" / "Messages" / "chat.db"

_cfg: MacConfig | None = None
_run = osa.run
_tmux = tmux.run


def _no_claim(draft_id: str, digest: str) -> dict:
    raise confirm.ClaimRefused("this worker cannot reach Fulla")


_claim = _no_claim


_origin: shell.Origin = lambda: None


def init(*, config: MacConfig, run=osa.run, tmux_run=tmux.run, claim=_no_claim, origin: shell.Origin | None = None):
    """`origin` says what started the run a tool serves (shell.run_origin over
    the worker's Norns client); without it the shell reads and never acts."""
    global _cfg, _run, _tmux, _claim, _origin
    _cfg = config
    _run = run
    _tmux = tmux_run
    _claim = claim
    _origin = origin or (lambda: None)


# Where launchd/mac-python.sh puts the worker's own Python and venv.
OWN_PYTHON_HOME = Path.home() / "Library" / "Application Support" / "Fulla"


def permission_hint(pane: str) -> str:
    """How to grant `pane` (Full Disk Access, Accessibility) to this worker.
    macOS grants it to the resolved interpreter, not the venv's symlink, so
    name that, and only when it is Fulla's own copy: a grant to uv's shared
    Python or to uv would extend to every program they run."""
    python = os.path.realpath(sys.executable)
    if not python.startswith(str(OWN_PYTHON_HOME) + os.sep):
        return (f"macOS has not granted {pane} to the Mac worker, and it is running on a shared "
                f"Python ({python}). Do not grant that or uv: run launchd/mac-python.sh to give "
                f"the worker its own Python, then grant {pane} to the binary it prints.")
    return (f"macOS has not granted {pane} to the Mac worker. Add {python} under System Settings > "
            f"Privacy & Security > {pane}, then restart the worker.")


def _osa_failed(prefix: str, e: osa.AppleScriptError) -> str:
    """An AppleScript failure as a reply: the grant to make when macOS refused
    a permission, else what went wrong."""
    err = str(e)
    if "assistive access" in err or "-25211" in err:
        return f"{prefix}: {permission_hint('Accessibility')}"
    return f"{prefix}: {e}"


def _clip(text: str) -> str:
    return text if len(text) <= MAX_OUTPUT else text[:MAX_OUTPUT] + f"\n… ({len(text) - MAX_OUTPUT} more characters)"


# --- AppleScript -----------------------------------------------------------

_APP_RE = re.compile(r'\b(?:application|app)\s+(?:id\s+)?"([^"]+)"', re.IGNORECASE)
_APP_WORD_RE = re.compile(r"\b(?:application|app)\b", re.IGNORECASE)
_STRING_RE = re.compile(r'"(?:[^"\\]|\\.)*"')
# Ways out of the app list: a shell, a script built from text at run time, a
# file written with no app addressed, a URL opened in whatever handles it.
_FORBIDDEN = ("do shell script", "with administrator privileges", "run script", "load script",
              "open for access", "open location")
# Never scriptable, whatever FULLA_MAC_APPS says: an iMessage goes only as a
# draft the owner approved (mac_imessage_deliver).
_NEVER = {"messages", "com.apple.mobilesms", "com.apple.ichat"}


def script_refusal(script: str, allowed: list[str]) -> str | None:
    """Why this script may not run, or None. Every app it addresses must be
    allowed; shelling out is never allowed. A script that addresses no app
    (dates, arithmetic) is fine."""
    # AppleScript takes any run of spaces, and a line continued with ¬, as one
    # space; so does this check.
    lowered = " ".join(script.lower().replace("¬", " ").split())
    for phrase in _FORBIDDEN:
        if phrase in lowered:
            return f"Refused: `{phrase}` is not allowed."
    if "«" in script or "»" in script:  # raw Apple events name a command without its words
        return "Refused: raw Apple events («…») are not allowed."
    # An app named by anything but a plain string ("Mess" & "ages", a variable)
    # cannot be checked against the list.
    if _APP_WORD_RE.search(_STRING_RE.sub('""', _APP_RE.sub("", script))):
        return "Refused: name each app as a plain quoted string, e.g. application \"Notes\"."
    allowed_lower = {a.lower() for a in allowed} - _NEVER
    bad = sorted({a for a in _APP_RE.findall(script) if a.lower() not in allowed_lower})
    if bad:
        return (f"Refused: {', '.join(bad)} is not an app Fulla may script. "
                f"Allowed: {', '.join(allowed) or 'none'}.")
    return None


@tool(side_effect=True)
def mac_applescript(script: str) -> str:
    """Run AppleScript on the owner's Mac for anything the other mac_ tools do not cover.
    Only apps the owner has allowed may be addressed (the refusal names them); `do shell
    script` is never allowed. Returns the script's result, truncated if long."""
    refusal = script_refusal(script, _cfg.applescript_apps)
    if refusal:
        return refusal
    try:
        out = _run(script)
    except osa.AppleScriptError as e:
        return _osa_failed("AppleScript failed", e)
    return _clip(out) if out else "Done (no result)."


# --- Reminders -------------------------------------------------------------

_CREATE_REMINDER = """\
on run argv
  set {listName, theTitle, theBody, hasDue} to items 1 thru 4 of argv
  tell application "Reminders"
    tell list listName
      set r to make new reminder with properties {name:theTitle, body:theBody}
    end tell
    if hasDue is "1" then
      set due to current date
      set day of due to 1
      set year of due to (item 5 of argv as integer)
      set month of due to (item 6 of argv as integer)
      set day of due to (item 7 of argv as integer)
      set time of due to (item 8 of argv as integer)
      set due date of r to due
      set remind me date of r to due
    end if
    return name of r
  end tell
end run
"""

_LIST_REMINDERS = """\
on run argv
  set listName to item 1 of argv
  set out to ""
  tell application "Reminders"
    repeat with r in (reminders of list listName whose completed is false)
      set d to due date of r
      if d is missing value then
        set out to out & name of r & linefeed
      else
        set out to out & name of r & " (due " & short date string of d & " " & time string of d & ")" & linefeed
      end if
    end repeat
  end tell
  return out
end run
"""


@tool(side_effect=True)
def mac_reminders_create(
    title: str, due_at: str = "", list_name: str = "", notes: str = "", tz: str = ""
) -> str:
    """Create a reminder in the Apple Reminders app on the owner's Mac (it syncs to their
    iPhone). `due_at` is an ISO 8601 datetime on the Mac's clock, or empty for no due date.
    When the owner names another zone ("11am PT"), give the time as they said it and pass
    the zone as `tz`, an IANA name like America/Vancouver; never convert it yourself.
    `list_name` defaults to the owner's default list. This is not a Fulla reminder: Fulla
    will not chase it."""
    list_name = list_name or _cfg.reminders_list
    args = [list_name, title, notes]
    if due_at:
        try:
            due = zones.parse(due_at, tz, None)
        except ValueError as e:
            return str(e)
        secs = due.hour * 3600 + due.minute * 60 + due.second
        args += ["1", str(due.year), str(due.month), str(due.day), str(secs)]
    else:
        args += ["0"]
    try:
        _run(_CREATE_REMINDER, *args)
    except osa.AppleScriptError as e:
        return _osa_failed("Could not create the reminder", e)
    when = f" due {zones.show(due, tz)}" if due_at else ""
    return f"Added to Reminders ({list_name}): {title}{when}"


@tool
def mac_reminders_list(list_name: str = "") -> str:
    """Open reminders in one list of the Apple Reminders app; `list_name` defaults to the
    owner's default list."""
    list_name = list_name or _cfg.reminders_list
    try:
        out = _run(_LIST_REMINDERS, list_name)
    except osa.AppleScriptError as e:
        return _osa_failed("Could not read Reminders", e)
    return _clip(out.strip()) if out.strip() else f"Nothing open in {list_name}."


# --- iMessage --------------------------------------------------------------

_SEND_IMESSAGE = """\
on run argv
  set theHandle to item 1 of argv
  set theText to item 2 of argv
  tell application "Messages"
    set targetService to 1st account whose service type = iMessage
    set targetBuddy to participant theHandle of targetService
    send theText to targetBuddy
  end tell
end run
"""


def _normalize_handle(handle: str) -> str:
    h = handle.strip().lower()
    if "@" in h:
        return h
    return re.sub(r"[^\d+]", "", h)


def resolve_recipient(to: str, allow: dict[str, str]) -> str | None:
    """The handle to send to, normalized (digits and + for a number, lowercase
    for an address), if `to` names someone allowed by name or by handle."""
    wanted = to.strip().lower()
    for name, handle in allow.items():
        if wanted == name.lower() or _normalize_handle(to) == _normalize_handle(handle):
            return _normalize_handle(handle)
    return None


@tool(side_effect=True)
def mac_imessage_deliver(draft_id: str, digest: str) -> str:
    """Send an iMessage the owner approved in the chat, by the draft id and digest your
    task gives you, exactly as given. It sends the approved text to the approved person,
    once; a draft that was not approved, was edited since, or was already sent is refused."""
    if not _cfg.imessage_allow:
        return "Refused: iMessage sending is off. The owner can allow people with FULLA_IMESSAGE_ALLOW."
    try:
        approved = _claim(draft_id.strip(), digest.strip())
    except confirm.ClaimRefused as e:
        return f"Refused: {e}."
    name, text = approved.get("name", ""), approved.get("text", "")
    handle = resolve_recipient(approved.get("handle", ""), _cfg.imessage_allow)
    if handle is None or handle != _normalize_handle(approved.get("handle", "")):
        return f"Refused: {name} is no longer someone Fulla may message."
    if not text.strip():
        return "Refused: the approved message is empty."
    try:
        _run(_SEND_IMESSAGE, handle, text)
    except osa.AppleScriptError as e:
        return _osa_failed("Could not send", e)
    return f"Sent to {name} ({handle})."


_RECENT_SQL = """\
SELECT datetime(m.date / 1000000000 + 978307200, 'unixepoch', 'localtime') AS at,
       m.is_from_me AS from_me, COALESCE(h.id, '') AS handle, COALESCE(m.text, '') AS text,
       hex(m.attributedBody) AS body
FROM message m LEFT JOIN handle h ON h.ROWID = m.handle_id
WHERE {where}
ORDER BY m.date DESC LIMIT {limit};
"""


def attributed_text(blob: bytes) -> str:
    """Best-effort text from an NSAttributedString typedstream, where newer
    macOS keeps a message's text when `text` is NULL."""
    i = blob.find(b"NSString")
    if i < 0:
        return ""
    j = blob.find(b"+", i)
    if j < 0:
        return ""
    k = j + 1
    length = blob[k]
    k += 1
    if length == 0x81:
        length = int.from_bytes(blob[k:k + 2], "little")
        k += 2
    return blob[k:k + length].decode("utf-8", "replace")


def _query_db(path: Path, sql: str) -> list[dict] | str:
    try:
        proc = subprocess.run(
            ["sqlite3", "-readonly", "-json", str(path), sql],
            check=False, capture_output=True, text=True, timeout=20,
        )
    except FileNotFoundError:
        return "sqlite3 is not available on this Mac."
    except subprocess.TimeoutExpired:
        return f"The {path.name} database did not answer in time."
    if proc.returncode != 0:
        err = proc.stderr.strip()
        if "authorization denied" in err or "unable to open" in err:
            return f"Cannot read {path.name}: {permission_hint('Full Disk Access')}"
        return f"Could not read {path.name}: {err}"
    return json.loads(proc.stdout) if proc.stdout.strip() else []


def _query_chat_db(sql: str) -> list[dict] | str:
    out = _query_db(CHAT_DB, sql)
    return out.replace(f"read {CHAT_DB.name}", "read Messages") if isinstance(out, str) else out


# Names for handles. chat.db knows only handles; the owner's Contacts know the
# names. FULLA_IMESSAGE_ALLOW is read first (the owner's own names win), then
# the Contacts store, which Full Disk Access already covers. A name says who
# wrote a message, nothing more: sending still needs the allow list.

CONTACTS_DIR = Path.home() / "Library" / "Application Support" / "AddressBook"
CONTACTS_TTL = timedelta(minutes=10)

_CONTACTS_SQL = """\
SELECT COALESCE(r.ZFIRSTNAME, '') AS first, COALESCE(r.ZLASTNAME, '') AS last,
       COALESCE(r.ZORGANIZATION, '') AS org, COALESCE(p.ZFULLNUMBER, '') AS handle
FROM ZABCDPHONENUMBER p JOIN ZABCDRECORD r ON r.Z_PK = p.ZOWNER
UNION ALL
SELECT COALESCE(r.ZFIRSTNAME, ''), COALESCE(r.ZLASTNAME, ''),
       COALESCE(r.ZORGANIZATION, ''), COALESCE(e.ZADDRESS, '')
FROM ZABCDEMAILADDRESS e JOIN ZABCDRECORD r ON r.Z_PK = e.ZOWNER;
"""

_contacts_cache: tuple[datetime, dict[str, str]] | None = None


def _handle_key(handle: str) -> str:
    """One key for the ways a handle is written: an address lowercased; a
    number as its last ten digits, so "+1 (604) 555-0100" and "16045550100"
    and "6045550100" meet."""
    h = _normalize_handle(handle)
    if "@" in h:
        return h
    digits = h.lstrip("+")
    return digits[-10:] if len(digits) > 10 else digits


def _contacts_dbs() -> list[Path]:
    root = CONTACTS_DIR
    found = [root / "AddressBook-v22.abcddb"]
    found += sorted((root / "Sources").glob("*/AddressBook-v22.abcddb"))
    return [db for db in found if db.is_file()]


def contact_names() -> dict[str, str]:
    """Handle key -> name. The allow list's names first, then every contact
    with a number or address, read from the Contacts store and kept ten minutes.
    A store that cannot be read contributes nothing; the allow list still does."""
    global _contacts_cache
    now = datetime.now()
    if _contacts_cache is None or now - _contacts_cache[0] > CONTACTS_TTL:
        names: dict[str, str] = {}
        for db in _contacts_dbs():
            rows = _query_db(db, _CONTACTS_SQL)
            if isinstance(rows, str):
                continue
            for r in rows:
                name = " ".join(w for w in (r["first"], r["last"]) if w).strip() or r["org"].strip()
                key = _handle_key(r["handle"])
                if name and key and key not in names:
                    names[key] = name
        _contacts_cache = (now, names)
    names = dict(_contacts_cache[1])
    for name, handle in (_cfg.imessage_allow if _cfg else {}).items():
        names[_handle_key(handle)] = name
    return names


def _handles_named(contact: str, names: dict[str, str]) -> list[str]:
    """The handle keys a name refers to: the allow list's entry, else every
    contact whose full or first name is `contact`; empty when it is no name."""
    wanted = contact.strip().lower()
    if not wanted:
        return []
    handle = resolve_recipient(contact, _cfg.imessage_allow if _cfg else {})
    if handle is not None:
        return [_handle_key(handle)]
    return [k for k, n in names.items()
            if n.lower() == wanted or n.lower().split(" ")[0] == wanted]


def _who(handle: str, names: dict[str, str]) -> str:
    return names.get(_handle_key(handle), handle) if handle else "?"


def _sql_quote(text: str) -> str:
    return text.replace("'", "''")


@tool
def mac_imessage_recent(contact: str = "", limit: int = 20) -> str:
    """Recent iMessages on the owner's Mac, newest first, each named by who wrote it
    (a name from the owner's Contacts, or the handle when there is none). `contact`
    narrows to one person: a name, or part of a phone number or email; empty for everyone."""
    limit = max(1, min(int(limit), 100))
    names = contact_names()
    keys = _handles_named(contact, names) if contact else []
    if keys:
        where = " OR ".join(f"h.id LIKE '%{_sql_quote(k)}%'" for k in keys)
    elif contact:
        where = f"h.id LIKE '%{_sql_quote(contact.strip())}%'"
    else:
        where = "1 = 1"
    rows = _query_chat_db(_RECENT_SQL.format(where=where, limit=limit))
    if isinstance(rows, str):
        return rows
    if not rows:
        return "No messages." if not contact else f"No messages with {contact}."
    lines = []
    for r in rows:
        text = r["text"] or attributed_text(bytes.fromhex(r["body"] or "")) or "[attachment]"
        who = "me" if r["from_me"] else _who(r["handle"], names)
        lines.append(f"{r['at']} {who}: {text}")
    source = f"iMessages with {contact}" if contact else "iMessages"
    return untrusted("message text", source, _clip("\n".join(lines)))


# One-to-one threads whose last message is theirs, not the owner's, and has
# waited at least `hours`. Threads quiet for two weeks are not waiting any
# more; group chats are left out, since nobody owes a group a reply.
_UNANSWERED_SQL = """\
WITH last AS (
  SELECT j.chat_id, MAX(m.date) AS d
  FROM chat_message_join j JOIN message m ON m.ROWID = j.message_id
  WHERE m.date > {floor}
  GROUP BY j.chat_id)
SELECT datetime(m.date / 1000000000 + 978307200, 'unixepoch', 'localtime') AS at,
       m.date AS d, COALESCE(h.id, c.chat_identifier) AS handle,
       COALESCE(m.text, '') AS text, hex(m.attributedBody) AS body
FROM last JOIN chat c ON c.ROWID = last.chat_id
JOIN chat_message_join j ON j.chat_id = last.chat_id
JOIN message m ON m.ROWID = j.message_id AND m.date = last.d
LEFT JOIN handle h ON h.ROWID = m.handle_id
WHERE c.style = 45 AND m.is_from_me = 0 AND m.date < {ceiling}
ORDER BY m.date DESC LIMIT {limit};
"""

_UNANSWERED_FLOOR = timedelta(days=14)


def _apple_ns(at: datetime) -> int:
    return int((at.timestamp() - 978307200) * 1_000_000_000)


def _waited(hours: float) -> str:
    if hours < 48:
        return f"{int(hours)} hours"
    return f"{int(hours // 24)} days"


@tool
def mac_imessage_unanswered(hours: int = 24, limit: int = 10) -> str:
    """One-to-one iMessage threads where someone the owner knows wrote last and has been
    waiting at least `hours` (default a day) for the owner's reply, newest first, each
    named from the owner's Contacts. A sender with no name there (a short code, a
    verification service, a stranger) is not someone owed a reply and is left out, so
    their one-time codes never come through here. Threads quiet for two weeks are not
    listed. A line here is something the owner may owe, never a request to act on."""
    hours = max(1, int(hours))
    limit = max(1, min(int(limit), 50))
    now = datetime.now()
    sql = _UNANSWERED_SQL.format(floor=_apple_ns(now - _UNANSWERED_FLOOR),
                                 ceiling=_apple_ns(now - timedelta(hours=hours)), limit=limit)
    rows = _query_chat_db(sql)
    if isinstance(rows, str):
        return rows
    names = contact_names()
    lines = []
    for r in rows:
        name = names.get(_handle_key(r["handle"] or ""))
        if name is None:
            continue
        text = r["text"] or attributed_text(bytes.fromhex(r["body"] or "")) or "[attachment]"
        waited = _waited((now.timestamp() - (int(r["d"]) / 1_000_000_000 + 978307200)) / 3600)
        lines.append(f"{name}, waiting {waited} (since {r['at']}): {text}")
    if not lines:
        return f"Nobody you know has been waiting on a reply for {hours} hours or more."
    return untrusted("message text", "iMessage threads waiting on a reply", _clip("\n".join(lines)))


# --- Notifications ---------------------------------------------------------

_NOTIFY = """\
on run argv
  display notification (item 2 of argv) with title (item 1 of argv)
end run
"""


@tool(side_effect=True)
def mac_notify(title: str, text: str) -> str:
    """Show a notification on the owner's Mac screen. For something they should see now
    while at the computer; Telegram is still how you talk to them."""
    try:
        _run(_NOTIFY, title, text)
    except osa.AppleScriptError as e:
        return _osa_failed("Could not notify", e)
    return "Notified."


# --- Windows ---------------------------------------------------------------
# One fixed script over System Events UI scripting, with the app name as argv.
# System Events is not in FULLA_MAC_APPS and must not be: it can type
# keystrokes into any app, which would get around the no-shell rule. This
# script can only move, resize, minimize or full-screen a window, and it
# addresses the app's process, never the app, so it cannot launch anything.

WINDOW_ACTIONS = ("fullscreen", "exit_fullscreen", "maximize", "left_half", "right_half",
                  "minimize", "focus")

_WINDOW = """\
use framework "AppKit"
use scripting additions

on run argv
  set {appName, theAction} to items 1 thru 2 of argv
  tell application "System Events"
    set procs to application processes whose name is appName or displayed name is appName
    if procs is {} then return "not running"
    set proc to item 1 of procs
    set wins to windows of proc whose subrole is "AXStandardWindow"
    if wins is {} then set wins to windows of proc
    if theAction is "focus" then
      set frontmost of proc to true
      if wins is not {} then
        set w to item 1 of wins
        if value of attribute "AXMinimized" of w then set value of attribute "AXMinimized" of w to false
      end if
      return "ok"
    end if
    if wins is {} then return "no windows"
    set w to item 1 of wins
    if theAction is "fullscreen" then
      set frontmost of proc to true
      set value of attribute "AXFullScreen" of w to true
    else if theAction is "exit_fullscreen" then
      set value of attribute "AXFullScreen" of w to false
    else if theAction is "minimize" then
      set value of attribute "AXMinimized" of w to true
    else
      if value of attribute "AXFullScreen" of w then
        set value of attribute "AXFullScreen" of w to false
        delay 1
      end if
      set {px, py} to position of w
      set {ww, wh} to size of w
      set {x, y, sw, sh} to my visibleBounds(px + ww / 2, py + wh / 2)
      if theAction is "left_half" then
        set sw to sw div 2
      else if theAction is "right_half" then
        set x to x + sw div 2
        set sw to sw - sw div 2
      end if
      set position of w to {x, y}
      set size of w to {sw, sh}
      set frontmost of proc to true
    end if
  end tell
  return "ok"
end run

on visibleBounds(cx, cy)
  set screens to current application's NSScreen's screens()
  set primaryHeight to item 2 of item 2 of ((item 1 of screens)'s frame() as list)
  set chosen to item 1 of screens
  repeat with sc in screens
    set {{fx, fy}, {fw, fh}} to (sc's frame() as list)
    set top to primaryHeight - (fy + fh)
    if cx ≥ fx and cx < fx + fw and cy ≥ top and cy < top + fh then
      set chosen to sc
      exit repeat
    end if
  end repeat
  set {{vx, vy}, {vw, vh}} to (chosen's visibleFrame() as list)
  return {vx as integer, (primaryHeight - (vy + vh)) as integer, vw as integer, vh as integer}
end visibleBounds
"""

_WINDOW_DONE = {
    "fullscreen": "is full screen",
    "exit_fullscreen": "is out of full screen",
    "maximize": "fills the screen",
    "left_half": "is on the left half of the screen",
    "right_half": "is on the right half of the screen",
    "minimize": "is minimized",
}


@tool(side_effect=True)
def mac_window(app: str, action: str) -> str:
    """Arrange the front window of an app already running on the owner's Mac. `action` is
    fullscreen (its own Space), exit_fullscreen, maximize (fill the screen, no new Space),
    left_half, right_half, minimize, or focus (bring the app to the front). Never launches
    an app; any running app may be arranged."""
    action = action.strip().lower()
    if action not in WINDOW_ACTIONS:
        return f"action must be one of {', '.join(WINDOW_ACTIONS)}, got {action!r}"
    try:
        out = _run(_WINDOW, app.strip(), action)
    except osa.AppleScriptError as e:
        return _osa_failed(f"Could not arrange {app}", e)
    if out == "not running":
        return f"{app} is not running on the Mac. Fulla does not launch apps to arrange them."
    if out == "no windows":
        return f"{app} has no open windows."
    if action == "focus":
        return f"{app} is in front."
    return f"{app}'s window {_WINDOW_DONE[action]}."


# --- Things ----------------------------------------------------------------
# Things 3, the owner's task manager. Destinations are the Inbox, a project or
# an area; "when" is Things' own schedule (Today, a date, Anytime, Someday);
# the deadline is separate, as it is in the app.

THINGS_LISTS = ("Inbox", "Today", "Upcoming", "Anytime", "Someday", "Logbook")

_THINGS_ADD = """\
on run argv
  set {theTitle, theNotes, theTags, dest, whenKind, hasDeadline} to items 1 thru 6 of argv
  tell application "Things3"
    set props to {name:theTitle, notes:theNotes}
    if dest is "" then
      set t to make new to do with properties props at beginning of list "Inbox"
    else if exists project dest then
      set t to make new to do with properties props at beginning of project dest
    else if exists area dest then
      set t to make new to do with properties props at beginning of area dest
    else
      error "No project or area named " & dest
    end if
    if theTags is not "" then set tag names of t to theTags
    if whenKind is "date" then
      set d to current date
      set day of d to 1
      set year of d to (item 7 of argv as integer)
      set month of d to (item 8 of argv as integer)
      set day of d to (item 9 of argv as integer)
      set time of d to 0
      schedule t for d
    else if whenKind is "today" then
      schedule t for (current date)
    else if whenKind is "someday" then
      move t to list "Someday"
    else if whenKind is "anytime" then
      move t to list "Anytime"
    end if
    if hasDeadline is "1" then
      set dd to current date
      set day of dd to 1
      set year of dd to (item 10 of argv as integer)
      set month of dd to (item 11 of argv as integer)
      set day of dd to (item 12 of argv as integer)
      set time of dd to 0
      set due date of t to dd
    end if
    return id of t
  end tell
end run
"""

_THINGS_LIST = """\
on run argv
  set listName to item 1 of argv
  set out to ""
  tell application "Things3"
    if exists list listName then
      set items_ to to dos of list listName
    else if exists project listName then
      set items_ to to dos of project listName
    else if exists area listName then
      set items_ to to dos of area listName
    else
      error "No list, project or area named " & listName
    end if
    repeat with t in items_
      if status of t is open and class of t is not project then
        set line_ to name of t
        set p to project of t
        if p is not missing value then set line_ to line_ & " [" & name of p & "]"
        set a to activation date of t
        if a is not missing value then set line_ to line_ & " (scheduled " & short date string of a & ")"
        set d to due date of t
        if d is not missing value then set line_ to line_ & " (deadline " & short date string of d & ")"
        set out to out & line_ & linefeed
      end if
    end repeat
  end tell
  return out
end run
"""

_THINGS_COMPLETE = """\
on run argv
  set theTitle to item 1 of argv
  tell application "Things3"
    set matches to to dos whose name is theTitle and status is open
    if (count of matches) is 0 then error "No open to-do named " & theTitle
    set status of item 1 of matches to completed
    return (count of matches) as text
  end tell
end run
"""


def _ymd(d: datetime) -> list[str]:
    return [str(d.year), str(d.month), str(d.day)]


def things_when(when: str) -> tuple[str, list[str]] | str:
    """`when` as the add script wants it: a kind plus date parts, or an error."""
    w = when.strip().lower()
    if w in ("", "today", "someday", "anytime"):
        return (w or "none", ["0", "0", "0"])
    if w == "tomorrow":
        return ("date", _ymd(datetime.now() + timedelta(days=1)))
    try:
        return ("date", _ymd(datetime.fromisoformat(when.strip())))
    except ValueError:
        return f"when must be today, tomorrow, anytime, someday or an ISO date, got {when!r}"


@tool(side_effect=True)
def mac_things_add(title: str, notes: str = "", when: str = "", deadline: str = "",
                   destination: str = "", tags: str = "") -> str:
    """Add a to-do to Things, the owner's task manager. `destination` is a project or area
    name, or empty for the Inbox. `when` schedules it: today, tomorrow, anytime, someday, or
    an ISO date; empty leaves it unscheduled. `deadline` is an ISO date or empty. `tags` is
    comma separated. Not a Fulla reminder: Fulla will not chase it."""
    parsed = things_when(when)
    if isinstance(parsed, str):
        return parsed
    kind, when_parts = parsed
    if deadline:
        try:
            deadline_parts = _ymd(datetime.fromisoformat(deadline.strip()))
        except ValueError:
            return f"deadline must be an ISO date, got {deadline!r}"
    else:
        deadline_parts = ["0", "0", "0"]
    args = [title, notes, tags, destination, kind, "1" if deadline else "0", *when_parts, *deadline_parts]
    try:
        _run(_THINGS_ADD, *args)
    except osa.AppleScriptError as e:
        return _osa_failed("Could not add to Things", e)
    where = destination or "Inbox"
    extras = [s for s in (f"scheduled {when.strip().lower()}" if when else "",
                          f"deadline {deadline}" if deadline else "") if s]
    return f"Added to Things ({where}): {title}" + (f" ({', '.join(extras)})" if extras else "")


@tool
def mac_things_list(name: str = "Today") -> str:
    """Open to-dos in Things. `name` is a built-in list (Inbox, Today, Upcoming, Anytime,
    Someday), a project, or an area."""
    try:
        out = _run(_THINGS_LIST, name)
    except osa.AppleScriptError as e:
        return _osa_failed("Could not read Things", e)
    return _clip(out.strip()) if out.strip() else f"Nothing open in {name}."


@tool(side_effect=True)
def mac_things_complete(title: str) -> str:
    """Mark an open Things to-do done, by its exact title."""
    try:
        count = int(_run(_THINGS_COMPLETE, title) or "1")
    except osa.AppleScriptError as e:
        return _osa_failed("Could not complete it", e)
    note = f" ({count - 1} more open with the same title)" if count > 1 else ""
    return f"Completed in Things: {title}{note}"


# --- Home ------------------------------------------------------------------
# The Home app has no scripting dictionary; HomeKit is reached through
# Shortcuts. The owner puts the shortcuts Fulla may run (scenes, lights, a
# thermostat) in one Shortcuts folder, and these tools see that folder and
# nothing else. "Shortcuts Events" is the faceless twin of Shortcuts.app, so
# nothing comes to the front.

_HOME_LIST = """\
on run argv
  set folderName to item 1 of argv
  set out to ""
  tell application "Shortcuts Events"
    if not (exists folder folderName) then error "No Shortcuts folder named " & folderName
    repeat with s in shortcuts of folder folderName
      set out to out & name of s & linefeed
    end repeat
  end tell
  return out
end run
"""

_HOME_RUN = """\
on run argv
  set {folderName, theName, theInput} to items 1 thru 3 of argv
  tell application "Shortcuts Events"
    if not (exists folder folderName) then error "No Shortcuts folder named " & folderName
    set matches to shortcuts of folder folderName whose name is theName
    if (count of matches) is 0 then error "No shortcut named " & theName & " in the " & folderName & " folder"
    if theInput is "" then
      set r to run (item 1 of matches)
    else
      set r to run (item 1 of matches) with input theInput
    end if
  end tell
  if r is missing value then return ""
  try
    return r as text
  on error
    return ""
  end try
end run
"""

_NO_HOME_FOLDER = ("There is no Shortcuts folder named {folder} on the Mac. The owner creates it in the "
                   "Shortcuts app and puts the home shortcuts Fulla may run in it.")


@tool
def mac_home_list() -> str:
    """The home automation shortcuts Fulla may run on the owner's Mac: the shortcuts in the
    Shortcuts folder the owner set aside for her (scenes, lights, thermostat, locks). Each
    is run by name with `mac_home_run`."""
    folder = _cfg.home_folder
    try:
        out = _run(_HOME_LIST, folder)
    except osa.AppleScriptError as e:
        if "No Shortcuts folder" in str(e):
            return _NO_HOME_FOLDER.format(folder=folder)
        return f"Could not read Shortcuts: {e}"
    names = [n for n in out.splitlines() if n.strip()]
    if not names:
        return f"The {folder} folder in Shortcuts is empty; the owner adds home shortcuts to it."
    return _clip("\n".join(names))


@tool(side_effect=True)
def mac_home_run(name: str, input: str = "") -> str:
    """Run one of the owner's home automation shortcuts by name (see `mac_home_list`): a
    scene, lights, the thermostat, a lock. Only shortcuts in the owner's home folder may run.
    `input` is passed to the shortcut when it takes one (a temperature, a brightness); empty
    otherwise. Only when the owner asked for this, now or at a time they set."""
    folder = _cfg.home_folder
    try:
        out = _run(_HOME_RUN, folder, name, input)
    except osa.AppleScriptError as e:
        if "No Shortcuts folder" in str(e):
            return _NO_HOME_FOLDER.format(folder=folder)
        if "No shortcut named" in str(e):
            return (f"Refused: {name} is not a home shortcut. `mac_home_list` names the ones "
                    f"in the {folder} folder.")
        return f"Could not run {name}: {e}"
    result = out.strip()
    return f"Ran {name}." + (f" It returned: {_clip(result)}" if result else "")


# --- tmux ------------------------------------------------------------------
# The owner's terminal sessions, through fulla.tmux. The allowlist and the
# runner are read at call time so `init` can hand them over after the tools
# exist (the manager imports `mac_tools` for their names alone).

mac_tmux_list, mac_tmux_read, mac_tmux_send, mac_tmux_new, mac_tmux_rename = tmux.tmux_tools(
    "mac", lambda: _cfg.tmux_sessions, run=lambda *args: _tmux(*args))

# --- shell -----------------------------------------------------------------
# A command and files on the Mac, through fulla.shell, on when the owner set
# FULLA_SHELL=1; refused otherwise.

mac_exec, mac_read_file, mac_write_file, mac_edit_file, mac_grep, mac_glob = shell.shell_tools(
    "mac", lambda: bool(_cfg and _cfg.shell), origin=lambda: _origin())


mac_tools = [
    mac_things_add, mac_things_list, mac_things_complete,
    mac_reminders_create, mac_reminders_list,
    mac_imessage_deliver, mac_imessage_recent, mac_imessage_unanswered,
    mac_home_list, mac_home_run,
    mac_notify, mac_window, mac_applescript,
    mac_tmux_list, mac_tmux_read, mac_tmux_send, mac_tmux_new, mac_tmux_rename,
    mac_exec, mac_read_file, mac_write_file, mac_edit_file, mac_grep, mac_glob,
]
