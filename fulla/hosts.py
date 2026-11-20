"""The machines Fulla knows: her memory of every worker that has ever
announced itself, so she can route a request to the right one.

A machine worker (the Mac's `fulla-mac`, a Linux box's `fulla-host`) serves
its tools under its own prefix, `mac_` or `<name>_`, and tells Fulla it
exists by POSTing to `/hosts` on the Fly app when it starts. Fulla keeps the
registry in `hosts.json`: the name, the platform, which tmux sessions it
allows, whether its shell is on, when it last announced, the machine's own id
(FULLA_MACHINE_ID, written by the installer) and what the owner said it is for.

The name is the routing: Norns dispatches `box_exec` to the one worker serving
it, so two machines with one name would be a coin toss. A name belongs to the
machine id that first announced it; another machine announcing the same name
is refused (NameTaken, 409) and its worker exits saying so. From it come the manager's allowlist
(every known machine's tools), the Machines section of the prompt, and the
per-turn note that says which machines are connected right now. Nothing is
configured on Fly: a new machine is known the first time it starts.

Whether a machine is connected at this moment is not in the registry; Norns
offers a machine's tools exactly while its worker is connected, so the
tool list on each turn is the truth, and the note spells it out.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import platform
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import httpx

from fulla import shell
from fulla.tmux import tool_names, valid_host

log = logging.getLogger("fulla.hosts")

MAC = "mac"


class NameTaken(ValueError):
    """The name belongs to another machine; the announcing worker must pick another."""


class Unreachable(Exception):
    """Fulla did not answer: the app asleep, the network down. Worth a retry."""


@dataclass
class Host:
    name: str
    platform: str
    sessions: list[str]
    worker_id: str
    seen_at: str
    # Who iMessage may go to, name -> handle; the Mac's FULLA_IMESSAGE_ALLOW,
    # so a draft names a handle the Mac will accept before the owner sees it.
    imessage: dict[str, str] = field(default_factory=dict)
    # Whether the owner turned the shell on there (FULLA_SHELL); the tools are
    # served and allowlisted either way, and refuse when it is off.
    shell: bool = False
    # The machine's own id (FULLA_MACHINE_ID); a name stays with the id that
    # took it. Empty for a worker installed before ids existed.
    machine_id: str = ""
    # What the owner said this machine is for, in their words, so "the dev
    # server" finds it. Set from the chat (describe_machine), never announced.
    purpose: str = ""

    @property
    def prefix(self) -> str:
        return f"{self.name}_"

    @property
    def label(self) -> str:
        return "the Mac" if self.name == MAC else f"the machine {self.name}"

    def tool_names(self) -> list[str]:
        """The tmux and shell tools this machine serves. The Mac's other tools
        are in `mac_tools`; these are among them under the same prefix."""
        return tool_names(self.name) + shell.tool_names(self.name)


def parse_announcement(data: dict) -> Host:
    """A Host from what a worker POSTed; ValueError says why it was refused."""
    name = str(data.get("name") or "").strip()
    if not valid_host(name):
        raise ValueError(f"name must be lowercase letters and digits, got {name!r}")
    sessions = data.get("sessions") or []
    if not isinstance(sessions, list) or not all(isinstance(s, str) for s in sessions):
        raise ValueError("sessions must be a list of names")
    imessage = data.get("imessage") or {}
    if not isinstance(imessage, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in imessage.items()):
        raise ValueError("imessage must map names to handles")
    return Host(
        name=name,
        platform=str(data.get("platform") or "unknown")[:40],
        sessions=[s.strip() for s in sessions if s.strip()][:50],
        worker_id=str(data.get("worker_id") or f"fulla-{name}")[:80],
        seen_at=datetime.now(UTC).isoformat(timespec="seconds"),
        imessage=dict(list(imessage.items())[:50]) if name == MAC else {},
        shell=bool(data.get("shell")),
        machine_id=str(data.get("machine_id") or "").strip()[:64],
    )


class HostStore:
    def __init__(self, path: Path):
        self.path = path
        self._items: dict[str, Host] = {}
        if path.exists():
            self._items = {h["name"]: Host(**h) for h in json.loads(path.read_text() or "[]")}

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps([asdict(h) for h in self.all()], indent=2))
        tmp.replace(self.path)

    def register(self, host: Host) -> bool:
        """Remember `host`. True when this changes what the manager may call
        or should be told: a new machine, or one whose allowed sessions, shell
        or platform moved. NameTaken when the name belongs to another machine:
        the one that announced it first keeps it until the owner forgets it."""
        before = self._items.get(host.name)
        if before is not None and before.machine_id and host.machine_id != before.machine_id:
            raise NameTaken(f"the name {host.name!r} belongs to another machine; give this one another "
                            f"FULLA_HOST, or have the owner tell Fulla to forget {host.name!r} first")
        if before is not None:
            host.purpose = before.purpose
            if not host.machine_id:
                host.machine_id = before.machine_id
        self._items[host.name] = host
        self._save()
        return (before is None or before.sessions != host.sessions or before.platform != host.platform
                or before.shell != host.shell)

    def describe(self, name: str, purpose: str) -> bool:
        """What the owner says a machine is for, kept with it across announcements."""
        host = self._items.get(name)
        if host is None:
            return False
        host.purpose = purpose.strip()[:200]
        self._save()
        return True

    def get(self, name: str) -> Host | None:
        return self._items.get(name)

    def forget(self, name: str) -> bool:
        if name not in self._items:
            return False
        del self._items[name]
        self._save()
        return True

    def all(self) -> list[Host]:
        return sorted(self._items.values(), key=lambda h: (h.name != MAC, h.name))

    def names(self) -> list[str]:
        """Every known machine besides the Mac, which is always known."""
        return [h.name for h in self.all() if h.name != MAC]

    def tool_names(self) -> list[str]:
        """The tmux and shell tools of every known machine besides the Mac."""
        names = []
        for h in self.all():
            if h.name != MAC:
                names += h.tool_names()
        return names

    def imessage_allow(self) -> dict[str, str]:
        """Who the Mac said iMessage may go to, as of its last start."""
        mac = self._items.get(MAC)
        return dict(mac.imessage) if mac else {}

    def render(self, owner: str) -> str:
        """The list of machines in the prompt's Machines section: each by prefix."""
        lines = []
        mac = self._items.get(MAC)
        mac_sessions = (", ".join(mac.sessions) or "none") if mac else "not announced yet"
        mac_shell = ("on" if mac.shell else "off") if mac else "not announced yet"
        mac_for = f", {mac.purpose}" if mac and mac.purpose else ""
        lines.append(f"`mac_` is {owner}'s Mac{mac_for} (tmux sessions allowed: {mac_sessions}; shell: {mac_shell}).")
        for h in self.all():
            if h.name == MAC:
                continue
            when = h.seen_at[:16].replace("T", " ")
            purpose = f", {h.purpose}" if h.purpose else ""
            lines.append(f"`{h.name}_` is {h.name}, a {h.platform} machine{purpose} "
                         f"(tmux sessions allowed: {', '.join(h.sessions) or 'none'}; "
                         f"shell: {'on' if h.shell else 'off'}; its worker last started {when} UTC).")
        return "The machines you know of, by the prefix of their tools:\n" + "\n".join(lines)


# --- The worker's side -------------------------------------------------------

def sign(secret: str, body: bytes, *, now: float | None = None) -> str:
    """The same Stripe-style header Norns uses (see signature.verify), with
    the Norns API key as the secret: both ends already hold it."""
    t = int(now if now is not None else time.time())
    mac = hmac.new(secret.encode(), f"{t}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={t},v1={mac}"


def announce(url: str, api_key: str, **kw) -> bool:
    """Tell Fulla this machine exists. Best effort: a machine whose worker is
    up serves its tools either way, and the manager may already know it. The
    one answer that is not best effort is 409, NameTaken: the name is another
    machine's, and serving its tools would be a coin toss."""
    try:
        return _announce(url, api_key, **kw)
    except Unreachable as e:
        log.warning("could not announce %s to Fulla at %s: %s", kw.get("name"), url, e)
        return False


def _announce(url: str, api_key: str, *, name: str, sessions: list[str], worker_id: str,
              system: str | None = None, imessage: dict[str, str] | None = None,
              shell: bool = False, machine_id: str = "", timeout: float = 20.0) -> bool:
    if not url:
        log.info("FULLA_URL is not set; not announcing %s to Fulla", name)
        return False
    body = json.dumps({
        "name": name, "sessions": sessions, "worker_id": worker_id,
        "platform": system or platform.system().lower() or "unknown",
        "imessage": imessage or {}, "shell": bool(shell), "machine_id": machine_id or "",
    }).encode()
    try:
        resp = httpx.post(url.rstrip("/") + "/hosts", content=body, timeout=timeout,
                          headers={"Content-Type": "application/json", "X-Norns-Signature": sign(api_key, body)})
    except httpx.HTTPError as e:
        raise Unreachable(str(e) or type(e).__name__) from None
    if resp.status_code == 409:
        try:
            why = resp.json().get("error") or resp.text[:200]
        except ValueError:
            why = resp.text[:200]
        raise NameTaken(str(why))
    if resp.status_code != 200:
        log.warning("Fulla refused the announcement of %s: %s %s", name, resp.status_code, resp.text[:200])
        return False
    log.info("announced %s to Fulla; machines known: %s", name, ", ".join(resp.json().get("known", [])))
    return True


RETRY_TAKEN = 3600.0  # seconds between tries while the name is another machine's
RETRY_UNREACHABLE = (10.0, 30.0, 60.0, 120.0)  # a Fly app asleep wakes on the first try and answers the next


def announce_until_named(url: str, api_key: str, *, sleep=time.sleep, wait: float = RETRY_TAKEN,
                         backoff: tuple[float, ...] = RETRY_UNREACHABLE, **kw) -> bool:
    """Announce, and while the name is another machine's, wait and try again
    rather than exit: launchd and systemd restart an exited worker every ten
    seconds, each try wakes the Fly app, and nobody is the wiser. The owner is
    told in chat by Fulla's side (main.on_host); here the log says why the
    worker sits idle. Serving tools meanwhile would be the coin toss, so the
    caller joins Norns only once this returns.

    When Fulla does not answer at all (the Fly app asleep: the first request
    wakes it and times out), try again a few times with a growing pause, then
    give up for this start: the machine serves its tools either way, and the
    next start announces again."""
    tries = iter(backoff)
    while True:
        try:
            return _announce(url, api_key, **kw)
        except NameTaken as e:
            log.error("not serving tools: %s. Trying again in %d minutes; tell Fulla to forget the name, "
                      "or give this machine another FULLA_HOST and restart.", e, int(wait // 60))
            sleep(wait)
            tries = iter(backoff)
        except Unreachable as e:
            pause = next(tries, None)
            if pause is None:
                log.warning("could not announce %s to Fulla at %s after %d tries (%s); serving anyway, and "
                            "the next start announces again", kw.get("name"), url, len(backoff) + 1, e)
                return False
            log.warning("could not announce %s to Fulla at %s (%s); trying again in %ds", kw.get("name"), url, e, int(pause))
            sleep(pause)


class Refusals:
    """Which taken names have been refused lately, so the owner hears about
    each once a day, not once per retry."""

    def __init__(self, every: float = 86400.0, clock=time.time):
        self.every = every
        self._clock = clock
        self._told: dict[str, float] = {}

    def should_tell(self, name: str) -> bool:
        now = self._clock()
        last = self._told.get(name)
        if last is not None and now - last < self.every:
            return False
        self._told[name] = now
        return True
