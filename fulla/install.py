"""`fulla-mac install` and `fulla-host install`: a machine worker set up from a
checkout in one command.

A machine needs the worker, a few values (the Norns URL and key, Fulla's URL,
which tmux sessions she may reach, and on a non-Mac machine its name) and a
service that keeps the worker up and pulls `main` hourly. This asks for the
values it was not given as flags, writes them to ~/.config/fulla/<kind>.env,
renders the launchd plists (Mac) or the systemd user units (anything else)
from the templates in launchd/ and systemd/, and starts them. On the Mac it
first builds Fulla's own Python and venv (launchd/mac-python.sh), since the
macOS permissions go to that binary and nothing else; the grant itself is the
owner's, in System Settings, and the last thing printed says which binary.

Running it again is how a value changes: existing values are the defaults,
the service is rewritten and restarted. The checkout stays the deploy
location, as before: the update job pulls `main` into it.

Imports only the standard library, so the Mac's `python -m fulla.mac` can
borrow `parse_env` before anything loads norns.
"""

from __future__ import annotations

import argparse
import getpass
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
MAC_LABELS = ("com.fulla.mac", "com.fulla.mac.update")
HOST_UNITS = ("fulla-host.service", "fulla-host-update.service", "fulla-host-update.timer")
HOST_NAME = re.compile(r"^[a-z][a-z0-9]*$")


def parse_env(path: Path) -> dict[str, str]:
    """KEY=value lines, as `uv run --env-file` reads them: blank lines and #
    comments skipped, an optional `export`, one pair of quotes stripped."""
    values: dict[str, str] = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.removeprefix("export ").split("=", 1)
            key, value = key.strip(), value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            else:
                value = value.split(" #", 1)[0].rstrip()
            values[key] = value
    return values


@dataclass
class Values:
    norns_url: str
    norns_api_key: str
    fulla_url: str
    tmux_sessions: str = ""
    shell: bool = False  # FULLA_SHELL: <name>_exec and the file tools
    host: str = ""  # the host worker's name; empty on the Mac
    machine_id: str = ""  # FULLA_MACHINE_ID: made once, kept; the name stays with it
    # Everything else the earlier env file set (the iMessage allow list, the
    # app list, ...): carried over as it was, never asked for, never dropped.
    extras: dict[str, str] = field(default_factory=dict)


ASKED = ("NORNS_URL", "NORNS_API_KEY", "FULLA_URL", "FULLA_HOST", "FULLA_TMUX_SESSIONS", "FULLA_SHELL",
         "FULLA_MACHINE_ID")


def default_host_name() -> str:
    """This machine's hostname as a tool prefix: lowercase letters and digits,
    the first label only; empty when that leaves nothing usable."""
    name = re.sub(r"[^a-z0-9]", "", socket.gethostname().split(".")[0].lower())
    name = name.lstrip("0123456789")
    return "" if name in ("", "mac", "localhost") else name[:24]

# The optional keys the Mac worker reads, with the line that documents each.
MAC_OPTIONAL = (
    ("FULLA_MAC_APPS", "# Apps AppleScript may address. Default: Things3,Reminders,Notes,Calendar",
     "Things3,Reminders,Notes,Calendar,Messages"),
    ("FULLA_IMESSAGE_ALLOW", "# Who Fulla may iMessage, as Name=handle pairs. Empty means sending is off.",
     "Jane=+16475550100,Mom=mom@icloud.com"),
    ("FULLA_REMINDERS_LIST", "# Default list for mac_reminders_create.", "Reminders"),
    ("FULLA_MAC_HOME_FOLDER",
     "# The Shortcuts folder whose shortcuts Fulla may run for the home. Default: Fulla Home", "Fulla Home"),
)


def _quote(value: str) -> str:
    """A value as an env line's right-hand side: quoted when it has a space or
    a #, so parse_env (and uv) read back exactly what was written."""
    if not any(c in value for c in " #\"'"):
        return value
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def config_dir() -> Path:
    return Path.home() / ".config" / "fulla"


def env_path(kind: str) -> Path:
    return config_dir() / f"{kind}.env"


def mac_home() -> Path:
    return Path.home() / "Library" / "Application Support" / "Fulla"


def mac_python() -> Path:
    """The binary launchd/mac-python.sh builds, and so the one to grant."""
    return mac_home() / "python" / "bin" / f"python{os.environ.get('FULLA_PYTHON_VERSION', '3.13')}"


def render_env(kind: str, v: Values) -> str:
    """The env file: the values asked for, and the optional ones as comments
    with their defaults, so the file says what else can be set."""
    lines = [
        (f"# Fulla's {'Mac' if kind == 'mac' else 'host'} worker, written by `fulla-{kind} install`; "
         "run it again to change a value."),
        "",
        "# The hosted Norns runtime, by its public URL, and the same key the Fly app uses.",
        f"NORNS_URL={v.norns_url}",
        f"NORNS_API_KEY={v.norns_api_key}",
        "",
        "# The Fly app, so the worker can announce itself and Fulla learns this machine exists.",
        f"FULLA_URL={v.fulla_url}",
        "",
    ]
    if kind == "host":
        lines += [
            "# This machine's name: its tools are served as <name>_tmux_list and the rest.",
            f"FULLA_HOST={v.host}",
            "",
        ]
    lines += [
        "# tmux sessions Fulla may read and type into, by name, or * for all of them.",
        "# Typing into a shell runs commands, so this is empty (off) by default.",
        f"FULLA_TMUX_SESSIONS={_quote(v.tmux_sessions)}",
        "",
        "# 1 gives Fulla a shell and the files on this machine, as this user: <name>_exec,",
        "# <name>_read_file and the rest. Empty means off, and the tools refuse.",
        f"FULLA_SHELL={'1' if v.shell else ''}",
        "",
        "# This machine's own id, made by the installer and kept: its name stays with it, and",
        "# another machine announcing the same name is refused.",
        f"FULLA_MACHINE_ID={v.machine_id}",
    ]
    extras = dict(v.extras)
    if kind == "mac":
        for key, doc, example in MAC_OPTIONAL:
            line = f"{key}={_quote(extras.pop(key))}" if key in extras else f"# {key}={example}"
            lines += ["", doc, line]
    if extras:
        lines += ["", "# Also set in the earlier env file, carried over as it was."]
        lines += [f"{key}={_quote(value)}" for key, value in sorted(extras.items())]
    return "\n".join(lines) + "\n"


def render_mac(repo: Path, env_file: Path) -> dict[str, str]:
    """The two launchd plists, from the templates, by label."""
    subs = {"REPO": str(repo), "FULLA_HOME": str(mac_home()), "ENV_FILE": str(env_file)}
    return {label: _fill((repo / "launchd" / f"{label}.plist").read_text(), subs) for label in MAC_LABELS}


def render_host(repo: Path, env_file: Path, uv: str) -> dict[str, str]:
    """The systemd user units, from the templates, by file name."""
    subs = {"REPO": str(repo), "UV": uv, "ENV_FILE": str(env_file)}
    return {unit: _fill((repo / "systemd" / unit).read_text(), subs) for unit in HOST_UNITS}


def _fill(text: str, subs: dict[str, str]) -> str:
    for key, value in subs.items():
        text = text.replace(key, value)
    return text


def _write(path: Path, text: str, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    if mode is not None:
        path.chmod(mode)


def install_mac(v: Values, *, repo: Path = REPO, run=subprocess.run, out=print, sleep=time.sleep) -> Path:
    """Build Fulla's own Python, write the env file and both plists, (re)load
    them, and say which binary to grant. Returns the env file's path."""
    run(["sh", str(repo / "launchd" / "mac-python.sh")], check=True, cwd=repo)
    env_file = env_path("mac")
    _write(env_file, render_env("mac", v), 0o600)
    agents = Path.home() / "Library" / "LaunchAgents"
    uid = os.getuid()
    for label, text in render_mac(repo, env_file).items():
        plist = agents / f"{label}.plist"
        _write(plist, text)
        run(["launchctl", "bootout", f"gui/{uid}/{label}"], check=False, capture_output=True)
        # bootstrap on the heels of bootout can fail with "Input/output error"
        # while the old job is still being torn down; a retry a moment later works.
        for attempt in range(5):
            proc = run(["launchctl", "bootstrap", f"gui/{uid}", str(plist)], check=False, capture_output=True)
            if proc.returncode == 0:
                break
            if attempt == 4:
                raise SystemExit(f"launchctl bootstrap {plist} failed: "
                                 f"{(getattr(proc, 'stderr', b'') or b'').decode(errors='replace').strip()}")
            sleep(2)
        out(f"loaded {label}")
    out(f"wrote {env_file}")
    out("Grant Full Disk Access and Accessibility (System Settings > Privacy & Security, \"+\", "
        "Cmd-Shift-G to paste the path) to this binary and nothing else:")
    out(f"  {mac_python()}")
    out(f"then restart the worker: launchctl kickstart -k gui/{uid}/com.fulla.mac")
    out("Logs: /tmp/fulla-mac.log and /tmp/fulla-mac-update.log")
    return env_file


def install_host(v: Values, *, repo: Path = REPO, run=subprocess.run, out=print, uv: str | None = None) -> Path:
    """Write the env file and the systemd user units, reload, and enable the
    worker and its hourly update. Returns the env file's path."""
    uv = uv or shutil.which("uv") or str(Path.home() / ".local" / "bin" / "uv")
    env_file = env_path("host")
    _write(env_file, render_env("host", v), 0o600)
    units = Path.home() / ".config" / "systemd" / "user"
    for unit, text in render_host(repo, env_file, uv).items():
        _write(units / unit, text)
    run(["systemctl", "--user", "daemon-reload"], check=True)
    run(["systemctl", "--user", "enable", "--now", "fulla-host"], check=True)
    run(["systemctl", "--user", "restart", "fulla-host"], check=True)
    run(["systemctl", "--user", "enable", "--now", "fulla-host-update.timer"], check=True)
    out(f"wrote {env_file}")
    out(f"enabled fulla-host and fulla-host-update.timer in {units}")
    out(f"To keep it up while you are logged out: loginctl enable-linger {getpass.getuser()}")
    out("Logs: journalctl --user -u fulla-host -f")
    return env_file


def defaults(kind: str, repo: Path = REPO) -> dict[str, str]:
    """What an earlier install or the checkout's own env file already says."""
    for path in (env_path(kind), repo / f".env.{kind}"):
        if path.is_file():
            return parse_env(path)
    return {}


def _ask(prompt: str, default: str, *, secret: bool = False, interactive: bool) -> str:
    if not interactive:
        if default:
            return default
        raise SystemExit(f"{prompt} is not set and there is no terminal to ask on; pass it as a flag.")
    if secret:
        shown = " [keep the current one]" if default else ""
        value = getpass.getpass(f"{prompt}{shown}: ")
    else:
        shown = f" [{default}]" if default else ""
        value = input(f"{prompt}{shown}: ")
    return value.strip() or default


def gather(kind: str, args: argparse.Namespace, *, repo: Path = REPO, interactive: bool | None = None) -> Values:
    """The values, from flags first, then prompts with what is already set as the default."""
    if interactive is None:
        interactive = sys.stdin.isatty()
    had = defaults(kind, repo)
    sessions = args.tmux_sessions
    if sessions is None:
        # Optional, so an absent flag with no terminal means what was set before, or nothing.
        sessions = had.get("FULLA_TMUX_SESSIONS", "")
        if interactive:
            sessions = _ask("tmux sessions she may reach (comma-separated, empty for none)",
                            sessions, interactive=True)
    shell = args.shell
    if shell is None:
        shell = had.get("FULLA_SHELL", "").strip().lower() in ("1", "true", "yes", "on")
        if interactive:
            answer = _ask("give her a shell and the files on this machine, as this user? (y/n)",
                          "y" if shell else "n", interactive=True)
            shell = answer.strip().lower() in ("y", "yes", "1", "true", "on")
    v = Values(
        norns_url=args.norns_url or _ask("Norns URL", had.get("NORNS_URL", ""), interactive=interactive),  # pyright: ignore[reportArgumentType]
        norns_api_key=args.norns_api_key
        or _ask("Norns API key", had.get("NORNS_API_KEY", ""), secret=True, interactive=interactive),  # pyright: ignore[reportArgumentType]
        fulla_url=args.fulla_url or _ask("Fulla's URL", had.get("FULLA_URL", ""), interactive=interactive),  # pyright: ignore[reportArgumentType]
        tmux_sessions=sessions,
        shell=shell,
        machine_id=had.get("FULLA_MACHINE_ID", "").strip() or uuid.uuid4().hex[:16],
        extras={k: val for k, val in had.items() if k not in ASKED},
    )
    if kind == "host":
        v.host = args.host or _ask("this machine's name (lowercase letters and digits)",
                                   had.get("FULLA_HOST", "") or default_host_name(), interactive=interactive)  # pyright: ignore[reportArgumentType]
        if not HOST_NAME.match(v.host):
            raise SystemExit(f"{v.host!r} is not a host name: lowercase letters and digits, starting with a letter.")
        if v.host == "mac":
            raise SystemExit("mac is the Mac worker's name; run fulla-mac install there instead.")
    for name, value in (("Norns URL", v.norns_url), ("Fulla's URL", v.fulla_url)):
        if not value.startswith(("http://", "https://")):
            raise SystemExit(f"{name} must start with http:// or https://, not {value!r}.")
    if not v.norns_api_key:
        raise SystemExit("A Norns API key is required.")
    return v


def parser(kind: str) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog=f"fulla-{kind} install",
                                description=f"Set up Fulla's {kind} worker from this checkout.")
    p.add_argument("--norns-url", help="the hosted Norns runtime")
    p.add_argument("--norns-api-key", help="its API key (prompted for, hidden, when not given)")
    p.add_argument("--fulla-url", help="the Fly app, e.g. https://fulla.fly.dev")
    p.add_argument("--tmux-sessions", help="sessions she may reach, comma-separated; empty for none")
    p.add_argument("--shell", dest="shell", action="store_true", default=None,
                   help="give her a shell and the files on this machine, as this user")
    p.add_argument("--no-shell", dest="shell", action="store_false", help="keep the shell off")
    if kind == "host":
        p.add_argument("--host", help="this machine's name, lowercase letters and digits")
    return p


def main(kind: str, argv: list[str] | None = None) -> int:
    args = parser(kind).parse_args(argv)
    v = gather(kind, args)
    if kind == "mac":
        if sys.platform != "darwin":
            raise SystemExit("fulla-mac install is for a Mac; on this machine run fulla-host install.")
        install_mac(v)
    else:
        install_host(v)
    return 0
