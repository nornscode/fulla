"""Entrypoints.

  fulla-serve   production: one process, one port, asleep between requests.
                The worker incarnation loop runs in the main thread; the HTTP
                server (wake, Telegram, Signal, SMS, run events) in the
                background. Telegram's and Twilio's webhooks are set at
                startup; on Signal, the forwarder beside signal-cli POSTs to
                /signal.
  fulla         local dev: the SDK worker runs all the time in the main
                thread; each messenger is polled (Telegram: long-poll, so a
                second bot token, since Telegram allows webhook or polling
                per bot; Signal: the receive stream; SMS: Twilio's message
                list every few seconds); run events still arrive over HTTP
                from the local Norns.

Every messenger with credentials in config is on; `channel.primary`
(FULLA_CHANNEL) names the one that gets scheduled turns.

Environment beyond config.yaml:
  NORNS_WEBHOOK_SECRET   from fulla-register-channel (required)
  NORNS_WAKE_SECRET      from fulla-register (fulla-serve only)
  FULLA_PUBLIC_URL       this app's public base URL; defaults to
                         https://$FLY_APP_NAME.fly.dev on Fly
  PORT                   HTTP port (default 9000)
  NORNS_WORKER_ID        declared worker id (default fulla)
  WAKE_IDLE, WAKE_MAX_LIFETIME   seconds (default 60, 900)
"""

# tools.init leaves imessage_allow unannotated (its default, `dict`, types it), so
# passing hosts.imessage_allow reads as the wrong type; drop this once it is annotated.
# pyright: reportArgumentType=false

from __future__ import annotations

import logging
import os
import threading

from norns import NornsClient

from fulla import github_cards, imessage, linear_cards, runctx, tools
from fulla.calendar import Calendars
from fulla.channel import Channel, ChannelState
from fulla.coding import CodingGuard, Sandboxes
from fulla.config import Config, load_config
from fulla.directives import DirectiveStore
from fulla.facts import FactStore
from fulla.feedback import FeedbackStore
from fulla.gaps import GapStore
from fulla.groups import GroupStore
from fulla.hosts import HostStore, NameTaken, Refusals, parse_announcement
from fulla.norns_api import NornsApi
from fulla.people import PeopleStore
from fulla.server import serve
from fulla.signal import Signal
from fulla.signature import signal_secret_token, telegram_secret_token
from fulla.sms import Sms
from fulla.soul import Soul
from fulla.telegram import Telegram
from fulla.tools import group_reminders, mac_queue, reminders
from fulla.wake import Incarnations, declare_agent, run_incarnations
from fulla.worker import build_agents, group_agents, manager_prompt
from norns_app.github_app import GitHubApp

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
logger = logging.getLogger("fulla")


def _transports(config: Config) -> dict[str, Telegram | Signal | Sms]:
    """Every messenger config has credentials for, by name. The primary one
    must be among them."""
    transports: dict[str, Telegram | Signal | Sms] = {}
    if "telegram" in config.owners:
        transports["telegram"] = Telegram(config.telegram.bot_token, config.telegram.user_id)
    if "signal" in config.owners:
        transports["signal"] = Signal(config.signal.api_url, config.signal.number, config.signal.owner)
    if "sms" in config.owners:
        transports["sms"] = Sms(config.sms.account_sid, config.sms.auth_token, config.sms.number, config.sms.owner)
    if config.channel.primary not in transports:
        need = {"telegram": "FULLA_TELEGRAM_BOT_TOKEN and FULLA_TELEGRAM_USER_ID",
                "signal": "FULLA_SIGNAL_NUMBER and FULLA_SIGNAL_OWNER",
                "sms": "TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, FULLA_SMS_NUMBER and FULLA_SMS_OWNER"}[config.channel.primary]
        raise SystemExit(f"FULLA_CHANNEL is {config.channel.primary}, so {need} are required")
    return transports


def github_app(config: Config) -> GitHubApp | None:
    """The owner's GitHub App, when its id and key are both set. A key that
    will not load is logged and leaves stored tokens in use; it never stops
    the process, which has a chat to serve either way."""
    fu = config.fulla
    if not (fu.github_app_id and fu.github_app_key):
        return None
    try:
        return GitHubApp(fu.github_app_id, fu.github_app_key)
    except ValueError as e:
        logger.error("FULLA_GITHUB_APP_KEY: %s; using stored GitHub tokens", e)
        return None


def _setup(config: Config):
    transports = _transports(config)
    data_dir = config.fulla.data_dir
    data_dir.mkdir(parents=True, exist_ok=True)

    client = NornsClient(config.norns.runtime_url, api_key=config.norns.api_key)
    api = NornsApi(config.norns.runtime_url, config.norns.api_key)
    store = FactStore(data_dir / "facts.json")
    calendars = Calendars(config.fulla.calendars, config.fulla.timezone, path=data_dir / "calendars.json")
    directives = DirectiveStore(data_dir / "directives.json")
    feedback = FeedbackStore(data_dir / "feedback.json")
    gaps = GapStore(data_dir / "gaps.json")
    for g in gaps.migrate_feedback(feedback):
        logger.info("moved feedback #%s to gap #%s", g.from_feedback[0], g.id)
    hosts = HostStore(data_dir / "hosts.json")
    soul = Soul(data_dir / "soul.md")
    people = PeopleStore(data_dir / "people.json")
    groups = GroupStore(data_dir / "groups", secret=config.fulla.secret, github_app=github_app(config))

    def push_prompt():
        """The manager's definition on the server, re-rendered: its prompt
        (soul, directives, machines) and its allowlist (every known machine's tools)."""
        api.update_agent("fulla", system_prompt=manager_prompt(config, directives, hosts, soul, groups),
                         allowed_tools=build_agents(config, directives, hosts, soul, store=groups)[0].allowed_tools)  # pyright: ignore[reportArgumentType]

    def push_groups():
        """Every allowed group's agent on the server, created or re-rendered
        from its directory (prompt and allowlist), the way the manager's
        prompt is pushed when a directive changes. The next join does the
        same from scratch, so a failure here is not final."""
        for agent in group_agents(config, groups.sync(people), store=groups):
            declare_agent(config.norns.runtime_url, config.norns.api_key, agent)

    def push_sources():
        """A change to a directory: the groups' agents, and the manager's
        prompt, which renders the owner's own sources."""
        push_groups()
        push_prompt()

    refusals = Refusals()

    def on_host(data: dict) -> list[str]:
        host = parse_announcement(data)  # ValueError -> 400 from the server
        try:
            changed = hosts.register(host)
        except NameTaken:
            # The worker waits and retries hourly; the owner hears once a day, with the way out.
            if refusals.should_tell(host.name):
                channel.tell_owner(f"A machine calling itself {host.name} is trying to join, but that name "
                                   f"belongs to another machine I know. If it is a reinstall, tell me to "
                                   f"forget {host.name} and it will be in within the hour; otherwise give it "
                                   f"another name.")
            raise
        if changed:
            logger.info("machine %s announced itself (%s; tmux: %s; shell: %s); updating the manager",
                        host.name, host.platform, ", ".join(host.sessions) or "off", "on" if host.shell else "off")
            push_prompt()
        else:
            logger.info("machine %s is back", host.name)
        return [h.name for h in hosts.all()]

    channel = Channel(
        transports, api, ChannelState(data_dir / "channel.json"),  # pyright: ignore[reportArgumentType]
        owners=config.owners, primary=config.channel.primary, conversation_key=config.conversation_key,
        reminder_known=reminders.known, mac_task_known=mac_queue.known,
        group_reminder_known=group_reminders.known,
        people=people, owner_ids=config.owner_ids, owner_name=config.fulla.owner_name,
        social_cap=config.channel.social_daily_cap, group_context=config.channel.group_context,
        confirm_kinds=[imessage.kind(api), *github_cards.kinds(groups), linear_cards.kind(groups)],
        groups=groups, push_groups=push_groups,
    )
    runctx.speakers_from(channel.speakers_of)  # so a group reminder knows who asked for it
    runctx.origins_from(channel.social_origin_of)  # and which group it serves
    tools.init(client=client, store=store, calendars=calendars, directives=directives,
               feedback=feedback, gaps=gaps, soul=soul, push_prompt=push_prompt, config=config, people=people,
               propose=channel.propose, imessage_allow=hosts.imessage_allow, groups=groups,
               push_groups=push_sources, api=api, hosts=hosts, say=channel.say)
    agents = lambda: build_agents(config, directives, hosts, soul, groups.sync(people), store=groups)
    # Whose Bubbo sandbox is whose: held on the model's calls, since Bubbo serves the tools.
    coding = CodingGuard(groups, Sandboxes(data_dir / "sandboxes.json"))
    return transports, channel, directives, hosts, soul, on_host, agents, coding


def _secret(name: str) -> str:
    """A signing secret from the environment. Set but empty is refused: the
    webhook tokens are derived from it, and from nothing they are public."""
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"{name} is required and must not be empty")
    return value


def _public_url() -> str:
    url = os.environ.get("FULLA_PUBLIC_URL")
    if not url and os.environ.get("FLY_APP_NAME"):
        url = f"https://{os.environ['FLY_APP_NAME']}.fly.dev"
    if not url:
        raise SystemExit("FULLA_PUBLIC_URL is required off Fly")
    return url.rstrip("/")


def _reconcile(channel: Channel):
    try:
        channel.reconcile()
    except Exception:
        logger.exception("reconcile failed; carrying on")


def serve_main():
    config = load_config()
    transports, channel, _directives, hosts, _soul, on_host, agents, coding = _setup(config)
    norns_secret = _secret("NORNS_WEBHOOK_SECRET")
    tg_secret = telegram_secret_token(norns_secret)
    telegram, signal, texts = transports.get("telegram"), transports.get("signal"), transports.get("sms")
    incarnations = Incarnations()
    public = _public_url()

    server = serve(
        channel,
        port=int(os.environ.get("PORT", "9000")),
        telegram_secret=tg_secret if telegram else "",
        norns_secret=norns_secret,
        wake_secret=_secret("NORNS_WAKE_SECRET"),
        incarnations=incarnations,
        worker_id=os.environ.get("NORNS_WORKER_ID", "fulla"),
        host_secret=config.norns.api_key,
        on_host=on_host,
        signal_secret=signal_secret_token(norns_secret) if signal else "",
        sms_auth_token=config.sms.auth_token if texts else "",
        sms_url=f"{public}/sms" if texts else "",
        egdir_secret=os.environ.get("EGDIR_NOTIFY_SECRET", ""),
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()

    if telegram:
        telegram.set_webhook(f"{public}/telegram", tg_secret)  # pyright: ignore[reportAttributeAccessIssue, reportCallIssue]
        logger.info("telegram webhook -> %s/telegram", public)
    if signal:
        logger.info("signal: %s talks to %s; inbound at %s/signal",
                    config.signal.number, config.signal.owner_number, public)
    if texts:
        try:
            texts.set_webhook(f"{public}/sms")  # pyright: ignore[reportAttributeAccessIssue, reportCallIssue]
            logger.info("sms: %s texts %s; twilio webhook -> %s/sms", config.sms.number, config.sms.owner, public)
        except Exception:
            # Twilio keeps the last webhook it was given; a hiccup here is
            # not worth refusing to start over.
            logger.exception("could not set the twilio webhook")
    logger.info("primary address %s; standing conversation %s", config.primary_address, config.conversation_key)
    _reconcile(channel)
    logger.info("serving on :%d", server.server_port)

    try:
        run_incarnations(
            incarnations,
            url=config.norns.runtime_url,
            api_key=config.norns.api_key,
            agents=agents,
            worker_id=os.environ.get("NORNS_WORKER_ID", "fulla"),
            idle=float(os.environ.get("WAKE_IDLE", "60")),
            max_lifetime=float(os.environ.get("WAKE_MAX_LIFETIME", "900")),
            hosts=hosts.names,
            owner=config.fulla.owner_name,
            coding=coding,
        )
    except KeyboardInterrupt:
        # Fly autostop sends SIGINT to an idle machine. Nothing to drain.
        logger.info("stopped while asleep")


def main():
    """Local development: worker always on, the messenger polled."""
    from fulla.wake import ServingNorns

    config = load_config()
    transports, channel, _directives, hosts, _soul, on_host, agents, coding = _setup(config)
    norns_secret = _secret("NORNS_WEBHOOK_SECRET")

    server = serve(
        channel,
        port=int(os.environ.get("PORT", "9000")),
        telegram_secret="",
        norns_secret=norns_secret,
        host_secret=config.norns.api_key,
        on_host=on_host,
        egdir_secret=os.environ.get("EGDIR_NOTIFY_SECRET", ""),
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    if "telegram" in transports:
        transports["telegram"].delete_webhook()  # pyright: ignore[reportAttributeAccessIssue]
    stop = threading.Event()
    for transport in transports.values():
        threading.Thread(target=transport.poll, args=(channel.handle, stop), daemon=True).start()
    _reconcile(channel)
    logger.info("Fulla is up: polling %s, run events on :%d", ", ".join(transports), server.server_port)

    try:
        ServingNorns(config.norns.runtime_url, api_key=config.norns.api_key, hosts=hosts.names,
                     coding=coding, owner=config.fulla.owner_name).run(agents())
    finally:
        stop.set()


if __name__ == "__main__":
    main()
