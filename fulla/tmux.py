"""The tmux tools: the owner's terminal sessions on one machine, read and typed
into. Platform-neutral; the Mac worker and the Linux host worker both serve
them, under the machine's own prefix (`mac_tmux_read`, `box_tmux_read`), since
Norns dispatches a tool call by name to one connected worker.

Which sessions Fulla may see, type into, create and rename is a list in that
machine's config (FULLA_TMUX_SESSIONS), empty by default; "*" allows them all.
Typing into a shell is running commands, so `<host>_tmux_send` is the one tool
that can do what `do shell script` may not; the list is the whole guard. Owner
data (the text typed, the target, a session name) travels to tmux as argv,
never through a shell.

`<host>_tmux_new` takes no command to start the session with: tmux runs a
one-word command through the default shell, so it would be a second way to run
commands, by shell string. A new session starts its shell, and typing into it
is `<host>_tmux_send`, the one way.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from collections.abc import Callable

from norns.agent import ToolDef

from fulla.tooldef import tool

TIMEOUT = 10
MAX_LINES = 500
MAX_WAIT = 15.0
MAX_OUTPUT = 4000
HOST_RE = re.compile(r"^[a-z][a-z0-9]*$")
_KEY_RE = re.compile(r"^[A-Za-z0-9_+\-]+$")
_ESCAPE_RE = re.compile(r"\x1b\[([0-9;:?]*)([A-Za-z])|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?|\x1b[ -/]*[0-~]?")
_EXTENDED_COLOUR = {"38", "48", "58"}
_LIST_FORMAT = ("#{session_name}\t#{window_index}\t#{pane_index}\t#{window_name}\t"
                "#{pane_current_command}\t#{pane_current_path}\t#{?pane_active,*,}")


class TmuxError(Exception):
    pass


def run(*args: str) -> str:
    """Run one tmux command and return its stdout. Arguments travel as argv."""
    try:
        proc = subprocess.run(["tmux", *args], check=False, capture_output=True, text=True,
                              timeout=TIMEOUT)
    except FileNotFoundError:
        raise TmuxError("tmux is not installed on this machine") from None
    except subprocess.TimeoutExpired:
        raise TmuxError(f"tmux did not answer within {TIMEOUT}s") from None
    if proc.returncode != 0:
        err = proc.stderr.strip() or f"tmux exited {proc.returncode}"
        if "no server running" in err or "error connecting to" in err:
            raise TmuxError("no tmux server is running")
        raise TmuxError(err)
    return proc.stdout.rstrip("\n")


def allowed(session: str, allow: list[str]) -> bool:
    return "*" in allow or session in allow


def valid_host(name: str) -> bool:
    """A host name is a tool-name prefix: lowercase letters and digits."""
    return bool(HOST_RE.match(name))


def _faint_after(params: str, faint: bool) -> bool:
    """The faint state after one SGR sequence. 2 sets it; 22, 0 and no
    parameter clear it. An extended colour's arguments (38;5;n, 38;2;r;g;b)
    are skipped, so their 2 is not taken for faint."""
    parts = params.split(";")
    i = 0
    while i < len(parts):
        p = parts[i]
        if p in ("", "0", "22"):
            faint = False
        elif p == "2":
            faint = True
        elif p in _EXTENDED_COLOUR and i + 1 < len(parts):
            i += {"5": 2, "2": 4}.get(parts[i + 1], 0)
        i += 1
    return faint


def plain(line: str) -> str:
    """One line of `capture-pane -e` as plain text, with dimmed (faint) runs
    wrapped in «…». Claude Code dims the placeholder in its input box; without
    the mark it reads as text typed and not sent. Marked, not dropped, since
    dimmed text can be real content too."""
    runs: list[list] = []
    faint = False
    pos = 0
    for m in [*_ESCAPE_RE.finditer(line), None]:
        text = line[pos:m.start() if m else len(line)]
        if text:
            if runs and runs[-1][1] == faint:
                runs[-1][0] += text
            else:
                runs.append([text, faint])
        if m is None:
            break
        if m.group(2) == "m" and "?" not in m.group(1):
            faint = _faint_after(m.group(1), faint)
        pos = m.end()
    out = []
    for text, dim in runs:
        core = text.strip()
        if dim and core:
            start = text.index(core)
            text = f"{text[:start]}«{core}»{text[start + len(core):]}"
        out.append(text)
    return "".join(out)


def _clip(text: str) -> str:
    return text if len(text) <= MAX_OUTPUT else text[:MAX_OUTPUT] + f"\n… ({len(text) - MAX_OUTPUT} more characters)"


def tool_names(host: str) -> list[str]:
    return [f"{host}_tmux_{n}" for n in ("list", "read", "send", "new", "rename")]


def tmux_tools(host: str, sessions: Callable[[], list[str]], run: Callable[..., str] = run) -> list[ToolDef]:
    """The tools for `host`, named `<host>_tmux_*`. `sessions` is read on
    every call so a worker can hand over its config after the tools exist."""
    if not valid_host(host):
        raise ValueError(f"host name must be lowercase letters and digits, got {host!r}")

    def off() -> str | None:
        if not sessions():
            return f"Refused: tmux access on {host} is off. The owner can allow sessions with FULLA_TMUX_SESSIONS."
        return None

    def refused(session: str) -> str:
        return (f"Refused: tmux session {session!r} is not one Fulla may use on {host}. "
                f"Allowed: {', '.join(sessions())}.")

    def resolve(target: str) -> tuple[str, str | None]:
        """The session `target` is in, and why Fulla may not touch it (or
        None). The check is on the session tmux resolves the target to, not
        on how it was spelled."""
        refusal = off()
        if refusal:
            return "", refusal
        if not target.strip():
            return "", "target is required: a session, session:window, or session:window.pane."
        try:
            session = run("display-message", "-p", "-t", target, "#{session_name}")
        except TmuxError as e:
            return "", f"Could not find tmux target {target!r} on {host}: {e}"
        if not allowed(session, sessions()):
            return session, refused(session)
        return session, None

    def check(target: str) -> str | None:
        return resolve(target)[1]

    def bad_name(name: str) -> str | None:
        """Why `name` cannot be a new session's name, or None. tmux would
        quietly turn a `:` or `.` into `_`, and the allowlist would have
        checked a name that is not the one made."""
        if not name or name != name.strip() or any(c in name for c in ":.") or name.startswith("="):
            return f"Session names are plain words, without : or . or surrounding spaces; got {name!r}."
        if not allowed(name, sessions()):
            return refused(name)
        return None

    def capture(target: str, lines: int) -> str:
        lines = max(1, min(int(lines), MAX_LINES))
        out = run("capture-pane", "-p", "-e", "-t", target, "-S", f"-{lines - 1}")
        kept = [plain(line).rstrip() for line in out.split("\n")]
        while kept and not kept[-1]:
            kept.pop()
        while kept and not kept[0]:
            kept.pop(0)
        return "\n".join(kept)

    def tmux_list() -> str:
        refusal = off()
        if refusal:
            return refusal
        try:
            out = run("list-panes", "-a", "-F", _LIST_FORMAT)
        except TmuxError as e:
            return f"Could not list tmux on {host}: {e}"
        rows = []
        for line in out.split("\n"):
            if not line:
                continue
            session, window, pane, name, cmd, path, active = line.split("\t")
            if allowed(session, sessions()):
                rows.append(f"{session}:{window}.{pane}{active}  {name}  {cmd}  {path}")
        if not rows:
            return f"No tmux sessions Fulla may use are running on {host}."
        return _clip("\n".join(rows))

    def tmux_read(target: str, lines: int = 50) -> str:
        refusal = check(target)
        if refusal:
            return refusal
        try:
            out = capture(target, lines)
        except TmuxError as e:
            return f"Could not read {target} on {host}: {e}"
        return _clip(out) if out else f"{target} on {host} shows nothing."

    def tmux_send(target: str, text: str = "", keys: str = "", enter: bool = True,
                  wait: float = 1.0) -> str:
        refusal = check(target)
        if refusal:
            return refusal
        names = keys.split()
        bad = [k for k in names if not _KEY_RE.match(k)]
        if bad:
            return f"keys must be tmux key names like C-c or Up, got {', '.join(bad)}."
        if not text and not names:
            return "Nothing to send: give text, keys, or both."
        try:
            if text:
                run("send-keys", "-t", target, "-l", text)
            if text and enter:
                run("send-keys", "-t", target, "Enter")
            if names:
                run("send-keys", "-t", target, *names)
            time.sleep(max(0.0, min(float(wait), MAX_WAIT)))
            out = capture(target, 30)
        except TmuxError as e:
            return f"Could not type into {target} on {host}: {e}"
        sent = " and ".join(s for s in (repr(text) if text else "", " ".join(names)) if s)
        return f"Sent {sent} to {target} on {host}. Pane now shows:\n{_clip(out) if out else '(nothing)'}"

    def tmux_new(session: str, directory: str = "") -> str:
        refusal = off() or bad_name(session)
        if refusal:
            return refusal
        args = ["new-session", "-d", "-s", session]
        if directory:
            path = os.path.expanduser(directory)
            if not os.path.isdir(path):
                return f"No directory {directory!r} on {host}."
            args += ["-c", path]
        try:
            run(*args)
        except TmuxError as e:
            return f"Could not create tmux session {session!r} on {host}: {e}"
        return f"Created tmux session {session!r} on {host}, detached, running its shell."

    def tmux_rename(target: str, new_name: str) -> str:
        session, refusal = resolve(target)
        refusal = refusal or bad_name(new_name)
        if refusal:
            return refusal
        try:
            run("rename-session", "-t", f"={session}", new_name)
        except TmuxError as e:
            return f"Could not rename {session!r} on {host}: {e}"
        return f"Renamed tmux session {session!r} to {new_name!r} on {host}."

    list_doc = (
        f"The tmux sessions, windows and panes on {host} that Fulla may use, one pane per "
        f"line as `session:window.pane  window-name  running-command  directory`; a `*` "
        f"after the pane marks the active one. Use the first column as `target` for the "
        f"other {host}_tmux_ tools."
    )
    read_doc = (
        f"What a tmux pane on {host} currently shows: the last `lines` lines of its screen "
        f"and scrollback (default 50, at most {MAX_LINES}). `target` is a session name, or "
        f"session:window.pane from {host}_tmux_list. Read-only. Text in «…» is dimmed on "
        f"screen: placeholders and hints, not typed input."
    )
    send_doc = (
        f"Type into a tmux pane on {host}. `text` is typed literally and followed by Enter "
        f"unless `enter` is false; `keys` are tmux key names sent after it, space separated, "
        f"for what is not text: C-c, C-d, Up, Escape, Tab. Then waits `wait` seconds (at most "
        f"{MAX_WAIT:.0f}) and returns the last lines of the pane. Whatever runs in the pane "
        f"runs what you type, so only type what the owner asked for. In the pane, text in «…» "
        f"is dimmed: placeholders and hints, not typed input."
    )
    new_doc = (
        f"Create a detached tmux session on {host} named `session`, running its shell, in "
        f"`directory` if given (else the home directory). The name must be one Fulla may use "
        f"there. To run something in it, type it with {host}_tmux_send. Only when the owner "
        f"asked for it."
    )
    rename_doc = (
        f"Rename the tmux session `target` is in on {host} to `new_name`. Both the session "
        f"and the new name must be ones Fulla may use there. Only when the owner asked for it."
    )
    return [
        tool(tmux_list, name=f"{host}_tmux_list", description=list_doc),
        tool(tmux_read, name=f"{host}_tmux_read", description=read_doc),
        tool(tmux_send, name=f"{host}_tmux_send", description=send_doc, side_effect=True),
        tool(tmux_new, name=f"{host}_tmux_new", description=new_doc, side_effect=True),
        tool(tmux_rename, name=f"{host}_tmux_rename", description=rename_doc, side_effect=True),
    ]
