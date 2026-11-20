"""The Mac worker: `fulla-mac`, a tools-only Norns worker (see toolworker.py).

Runs under launchd on the owner's Mac (see launchd/com.fulla.mac.plist and
__main__.py).
Needs NORNS_URL (the hosted runtime's public URL) and NORNS_API_KEY. With
FULLA_URL set it also announces itself to Fulla on start (see hosts.py), so
Fulla knows which tmux sessions the Mac allows.
"""

from __future__ import annotations

import logging
import os
import sys

from norns import Agent

from fulla import shell
from fulla.config import load_config
from fulla.confirm import claim
from fulla.hosts import announce_until_named
from fulla.mac import tools
from fulla.mac.nudge import nudge
from fulla.norns_api import NornsApi
from fulla.toolworker import ToolWorker

WORKER_ID = "fulla-mac"
CARRIER = "fulla-mac"

log = logging.getLogger("fulla.mac")


def carrier(config, api: NornsApi | None = None) -> Agent:
    """`api` lets the shell ask Norns what started a run; without it the
    shell reads and never acts."""
    tools.init(config=config.mac, claim=lambda draft_id, digest: claim(
        config.fulla.url, config.norns.api_key, draft_id, digest),
        origin=shell.run_origin(api) if api is not None else None)
    return Agent(name=CARRIER, tools=tools.mac_tools)


def main():
    if sys.argv[1:2] == ["install"]:
        from fulla.install import main as install_main

        raise SystemExit(install_main("mac", sys.argv[2:]))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    config = load_config()
    api = NornsApi(config.norns.runtime_url, config.norns.api_key)
    agent = carrier(config, api)
    worker_id = os.environ.get("NORNS_WORKER_ID", WORKER_ID)
    log.info("serving %d mac tools as %s; AppleScript apps: %s; iMessage to: %s; tmux: %s; shell: %s; "
             "home shortcuts from: %s",
             len(agent.tools), worker_id, ", ".join(config.mac.applescript_apps),
             ", ".join(config.mac.imessage_allow) or "nobody",
             ", ".join(config.mac.tmux_sessions) or "off", "on" if config.mac.shell else "off",
             config.mac.home_folder)
    announce_until_named(config.fulla.url, config.norns.api_key, name="mac",
                         sessions=config.mac.tmux_sessions, worker_id=worker_id, system="mac",
                         imessage=config.mac.imessage_allow, shell=config.mac.shell,
                         machine_id=config.mac.machine_id)
    expected = [t.name for t in agent.tools]
    ToolWorker(config.norns.runtime_url, api_key=config.norns.api_key,
               on_join=lambda: nudge(api, expected)).run(agent, worker_id=worker_id)  # pyright: ignore[reportArgumentType]


if __name__ == "__main__":
    main()
