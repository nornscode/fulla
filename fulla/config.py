"""Configuration: config.yaml with ${VAR} and ${VAR:-default} interpolation."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from fulla.calendar import parse_feeds
from fulla.tmux import valid_host


@dataclass
class ChannelConfig:
    primary: str  # "telegram" | "signal": the messenger that gets what has no origin (scheduled turns)
    social_daily_cap: int = 50  # messages a day Fulla answers in any one friends' conversation or group
    group_context: int = 8  # group messages before an addressed one that go with it, as untrusted context; 0 for none


@dataclass
class SignalConfig:
    api_url: str
    number: str  # Fulla's, E.164
    owner: str  # who may talk to Fulla: number and/or account id, comma-separated

    @property
    def owner_number(self) -> str:
        """The owner's number, which is where Fulla sends: the first
        identifier that looks like one, else the first there is."""
        ids = [o.strip() for o in self.owner.split(",") if o.strip()]
        return next((o for o in ids if o.startswith("+")), ids[0] if ids else "")


@dataclass
class SmsConfig:
    """Text messages through Twilio. All four, or SMS is off."""

    account_sid: str
    auth_token: str
    number: str  # Fulla's, E.164
    owner: str  # the owner's, E.164: the only number whose texts Fulla reads


@dataclass
class TelegramConfig:
    bot_token: str
    user_id: int


@dataclass
class NornsConfig:
    runtime_url: str
    api_key: str


@dataclass
class FullaConfig:
    owner_name: str
    timezone: str
    data_dir: Path
    model: str
    calendars: dict[str, str]
    mid_model: str = ""  # the reader, the social and group agents, the Mac queue; empty means `model`
    light_model: str = ""  # reminders, follow-ups and the sandbox watcher; empty means `model`
    url: str = ""  # the Fly app's public URL, for machine workers to announce themselves to
    brave_api_key: str = ""  # web search; empty turns it off
    secret: str = ""  # encrypts groups' credentials at rest (FULLA_SECRET); empty means none can be stored
    github_app_id: str = ""  # a GitHub App's id (FULLA_GITHUB_APP_ID); with the key, it replaces stored GitHub tokens
    github_app_key: str = ""  # its private key, PEM (FULLA_GITHUB_APP_KEY)
    repo: str = ""  # Fulla's own repository, owner/name (FULLA_REPO); empty means Fulla is not told
    commit: str = ""  # the commit Fulla is running, baked in at build (FULLA_COMMIT); empty when unknown


@dataclass
class MacConfig:
    """Read only by the Mac worker (`fulla-mac`)."""

    applescript_apps: list[str]
    imessage_allow: dict[str, str]  # name -> iMessage handle
    reminders_list: str
    tmux_sessions: list[str] = field(default_factory=list)  # "*" means every session
    home_folder: str = "Fulla Home"  # the Shortcuts folder whose shortcuts mac_home_run may run
    shell: bool = False  # FULLA_SHELL=1: mac_exec and the file tools (shell.py) are on
    machine_id: str = ""  # FULLA_MACHINE_ID, written by the installer; the name "mac" stays with it


@dataclass
class HostConfig:
    """Read only by the host worker (`fulla-host`), on a machine that is not the Mac."""

    name: str  # the prefix its tools are served under; "" when this is not a host
    tmux_sessions: list[str] = field(default_factory=list)
    shell: bool = False  # FULLA_SHELL=1: <name>_exec and the file tools (shell.py) are on
    machine_id: str = ""  # FULLA_MACHINE_ID, written by the installer; the name stays with it


@dataclass
class Config:
    telegram: TelegramConfig
    norns: NornsConfig
    fulla: FullaConfig
    mac: MacConfig
    host: HostConfig
    channel: ChannelConfig = field(default_factory=lambda: ChannelConfig(primary="telegram"))
    signal: SignalConfig = field(default_factory=lambda: SignalConfig(api_url="", number="", owner=""))
    sms: SmsConfig = field(default_factory=lambda: SmsConfig(account_sid="", auth_token="", number="", owner=""))

    @property
    def owners(self) -> dict[str, int | str]:
        """The owner's chat id on each messenger that is configured. Any
        number may be: Fulla listens on every one and answers where it was
        spoken to."""
        owners: dict[str, int | str] = {}
        if self.telegram.bot_token and self.telegram.user_id:
            owners["telegram"] = self.telegram.user_id
        if self.signal.number and self.signal.owner:
            owners["signal"] = self.signal.owner_number
        if self.sms.account_sid and self.sms.auth_token and self.sms.number and self.sms.owner:
            owners["sms"] = self.sms.owner
        return owners

    @property
    def owner_ids(self) -> dict[str, set[str]]:
        """Every id the owner is known by on each messenger, as strings: the
        Telegram user id; the Signal number and account id, whichever were given."""
        ids: dict[str, set[str]] = {}
        if "telegram" in self.owners:
            ids["telegram"] = {str(self.telegram.user_id)}
        if "signal" in self.owners:
            ids["signal"] = {o.strip() for o in self.signal.owner.split(",") if o.strip()}
        if "sms" in self.owners:
            ids["sms"] = {self.sms.owner}
        return ids

    @property
    def primary_address(self) -> str:
        """Where scheduled turns and anything else without an origin go."""
        owners = self.owners
        if self.channel.primary not in owners:
            raise ValueError(f"channel primary is {self.channel.primary!r}, which is not configured")
        return f"{self.channel.primary}:{owners[self.channel.primary]}"

    @property
    def conversation_key(self) -> str:
        """The manager's standing conversation, built here and nowhere else:
        the channel chats in it from every address and the follow-up tool
        writes into it. Named after the primary address, so a one-messenger
        setup keeps the key it always had."""
        return self.primary_address


_ENV_VAR_RE = re.compile(r"\$\{(\w+)(?::-([^}]*))?\}")


def _interpolate(value: str) -> str:
    def replacer(match: re.Match) -> str:
        name, default = match.group(1), match.group(2)
        env_val = os.environ.get(name)
        if env_val is not None and env_val != "":
            return env_val
        if default is not None:
            return default
        raise ValueError(f"Environment variable {name} is not set")

    return _ENV_VAR_RE.sub(replacer, value)


def _interpolate_recursive(obj) -> Any:
    if isinstance(obj, str):
        return _interpolate(obj)
    if isinstance(obj, dict):
        return {k: _interpolate_recursive(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_interpolate_recursive(item) for item in obj]
    return obj


def _parse_pairs(spec: str, what: str) -> dict[str, str]:
    """'Name=value,Other=value' -> {name: value}. Empty spec -> {}."""
    pairs = {}
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        name, _, value = part.partition("=")
        if not value:
            raise ValueError(f"{what} entry needs Name=value, got {part!r}")
        pairs[name.strip()] = value.strip()
    return pairs


def _parse_list(spec: str) -> list[str]:
    return [item.strip() for item in spec.split(",") if item.strip()]


def _switch(value) -> bool:
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def load_config(path: str | Path = "config.yaml") -> Config:
    with open(path) as f:
        raw = yaml.safe_load(f)

    data = _interpolate_recursive(raw)
    tg, nr, fu = data.get("telegram") or {}, data["norns"], data["fulla"]
    mc, ho = data.get("mac") or {}, data.get("host") or {}
    ch, sg, sm = data.get("channel") or {}, data.get("signal") or {}, data.get("sms") or {}

    primary = (ch.get("primary") or ch.get("kind") or "telegram").strip().lower()
    if primary not in ("telegram", "signal", "sms"):
        raise ValueError(f"channel primary must be telegram, signal or sms, got {primary!r}")

    host = (ho.get("name") or "").strip()
    if host and not valid_host(host):
        raise ValueError(f"host name must be lowercase letters and digits, got {host!r}")

    return Config(
        telegram=TelegramConfig(bot_token=tg.get("bot_token") or "", user_id=int(tg.get("user_id") or 0)),
        norns=NornsConfig(runtime_url=nr["runtime_url"], api_key=nr["api_key"]),
        fulla=FullaConfig(
            owner_name=fu["owner_name"],
            timezone=fu["timezone"],
            data_dir=Path(fu["data_dir"]),
            model=fu["model"],
            mid_model=(fu.get("mid_model") or "").strip() or fu["model"],
            light_model=(fu.get("light_model") or "").strip() or fu["model"],
            calendars=parse_feeds(fu.get("calendars") or ""),
            url=(fu.get("url") or "").strip(),
            brave_api_key=(fu.get("brave_api_key") or "").strip(),
            secret=(fu.get("secret") or "").strip(),
            github_app_id=str(fu.get("github_app_id") or "").strip(),
            github_app_key=str(fu.get("github_app_key") or "").strip(),
            repo=(fu.get("repo") or "").strip(),
            commit=(fu.get("commit") or "").strip(),
        ),
        mac=MacConfig(
            applescript_apps=_parse_list(mc.get("applescript_apps") or ""),
            imessage_allow=_parse_pairs(mc.get("imessage_allow") or "", "imessage_allow"),
            reminders_list=mc.get("reminders_list") or "Reminders",
            tmux_sessions=_parse_list(mc.get("tmux_sessions") or ""),
            home_folder=mc.get("home_folder") or "Fulla Home",
            shell=_switch(mc.get("shell")),
            machine_id=str(mc.get("machine_id") or "").strip(),
        ),
        host=HostConfig(name=host, tmux_sessions=_parse_list(ho.get("tmux_sessions") or ""),
                        shell=_switch(ho.get("shell")), machine_id=str(ho.get("machine_id") or "").strip()),
        channel=ChannelConfig(primary=primary, social_daily_cap=int(ch.get("social_daily_cap") or 50),
                              group_context=int(ch.get("group_context") if ch.get("group_context") not in (None, "") else 8)),  # pyright: ignore[reportArgumentType]
        signal=SignalConfig(
            api_url=(sg.get("api_url") or "http://fulla-signal.internal:8080").strip(),
            number=(sg.get("number") or "").strip(),
            owner=(sg.get("owner") or "").strip(),
        ),
        sms=SmsConfig(
            account_sid=(sm.get("account_sid") or "").strip(),
            auth_token=(sm.get("auth_token") or "").strip(),
            number=(sm.get("number") or "").strip(),
            owner=(sm.get("owner") or "").strip(),
        ),
    )
