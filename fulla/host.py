"""The host worker: `fulla-host`, a tools-only Norns worker for any machine
that is not the Mac (a Linux box, say). It serves the tmux tools and the
shell tools under the machine's own name: FULLA_HOST=box serves `box_tmux_list`,
`box_tmux_read`, `box_tmux_send` and the rest, and `box_exec`, `box_read_file`
and the other file tools (on with FULLA_SHELL=1, refusing otherwise), and
joins Norns as the worker `fulla-box`. On start it announces itself to Fulla
at FULLA_URL (see hosts.py); that is how Fulla learns the machine exists, and
nothing is configured on Fly.

Runs under a systemd user service (see systemd/). Needs NORNS_URL,
NORNS_API_KEY, FULLA_URL, FULLA_HOST, FULLA_TMUX_SESSIONS and FULLA_SHELL;
`fulla-host install` asks for them and sets the service up (install.py).
"""

from __future__ import annotations

import logging
import sys

from norns import Agent

from fulla.config import Config, load_config
from fulla.hosts import announce_until_named
from fulla.norns_api import NornsApi
from fulla.shell import run_origin, shell_tools
from fulla.tmux import tmux_tools
from fulla.toolworker import ToolWorker

log = logging.getLogger("fulla.host")


def carrier(config: Config, api: NornsApi | None = None) -> Agent:
    """`api` lets the shell ask Norns what started a run; without it the
    shell reads and never acts."""
    host = config.host.name
    if not host:
        raise SystemExit("FULLA_HOST is not set: the name this machine's tools are served under.")
    if host == "mac":
        raise SystemExit("FULLA_HOST=mac is the Mac worker's name; run fulla-mac there instead.")
    origin = run_origin(api) if api is not None else (lambda: None)
    return Agent(name=f"fulla-{host}", tools=tmux_tools(host, lambda: config.host.tmux_sessions)
                 + shell_tools(host, lambda: config.host.shell, origin=origin))


def main():
    if sys.argv[1:2] == ["install"]:
        from fulla.install import main as install_main

        raise SystemExit(install_main("host", sys.argv[2:]))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    config = load_config()
    agent = carrier(config, NornsApi(config.norns.runtime_url, config.norns.api_key))
    log.info("serving %d tools as %s; tmux: %s; shell: %s", len(agent.tools), agent.name,
             ", ".join(config.host.tmux_sessions) or "off", "on" if config.host.shell else "off")
    if not config.fulla.url:
        log.warning("FULLA_URL is not set: Fulla will not learn this machine exists unless it already knows it")
    announce_until_named(config.fulla.url, config.norns.api_key, name=config.host.name,
                         sessions=config.host.tmux_sessions, worker_id=agent.name, shell=config.host.shell,
                         machine_id=config.host.machine_id)
    ToolWorker(config.norns.runtime_url, api_key=config.norns.api_key).run(agent, worker_id=agent.name)


if __name__ == "__main__":
    main()
