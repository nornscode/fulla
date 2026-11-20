"""The container's entrypoint: run the three long-lived processes and keep
them running.

Upstream's image starts its two services with s6-overlay, whose /init
refuses to run unless it is PID 1. On Fly it never is: Fly's own init holds
PID 1 and runs the entrypoint as a child. So this replaces s6 with the
plainest thing that does the same job:

  signal-cli-rest-api   the HTTP API on $PORT (8080)
  jsonrpc2-helper       writes jsonrpc2.yml and execs `signal-cli daemon`
                        on 127.0.0.1:6001, which the API talks to in
                        json-rpc mode (upstream's signal-json-rpc service)
  forward.py            Fulla's forwarder, signal-cli's receive stream in,
                        signed POSTs out

A child that exits is started again after a pause that widens to thirty
seconds, so a crash loop is visible in the logs but not hot. SIGTERM (a
deploy, a `fly machine restart`) stops the children and exits. Output is
inherited, so everything lands in `fly logs`. Standard library only.
"""

import logging
import os
import signal
import subprocess
import sys
import time

logging.basicConfig(level=logging.INFO, format="%(asctime)s run %(message)s", stream=sys.stdout)
log = logging.getLogger("run")

MAX_PAUSE = 30.0


def commands() -> dict[str, list[str]]:
    config_dir = os.environ.get("SIGNAL_CLI_CONFIG_DIR", "/home/.local/share/signal-cli")
    mode = os.environ.get("MODE", "normal")
    cmds = {
        "signal-api": ["signal-cli-rest-api", f"-signal-cli-config={config_dir}"],
        "forward": [sys.executable, "/usr/local/bin/forward.py"],
    }
    if mode in ("json-rpc", "json-rpc-native"):
        cmds["signal-json-rpc"] = ["jsonrpc2-helper"]
    else:
        log.info("MODE=%s: no json-rpc daemon", mode)
    return cmds


class Child:
    def __init__(self, name: str, cmd: list[str]):
        self.name, self.cmd = name, cmd
        self.proc: subprocess.Popen | None = None
        self.pause = 1.0
        self.started = 0.0
        self.restart_at = 0.0

    def start(self) -> None:
        log.info("starting %s: %s", self.name, " ".join(self.cmd))
        self.proc = subprocess.Popen(self.cmd)
        self.started = time.monotonic()

    def tend(self) -> None:
        """Start if not running; note an exit; restart when its pause is up."""
        now = time.monotonic()
        if self.proc is None:
            if now >= self.restart_at:
                self.start()
            return
        code = self.proc.poll()
        if code is None:
            if now - self.started > 60:
                self.pause = 1.0  # ran a while: not a crash loop
            return
        log.warning("%s exited with %s; restarting in %.0fs", self.name, code, self.pause)
        self.proc = None
        self.restart_at = now + self.pause
        self.pause = min(self.pause * 2, MAX_PAUSE)

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()

    def wait(self, timeout: float) -> None:
        if self.proc is not None:
            try:
                self.proc.wait(timeout)
            except subprocess.TimeoutExpired:
                self.proc.kill()


def main() -> None:
    children = [Child(name, cmd) for name, cmd in commands().items()]
    stopping = False

    def on_term(signum, frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)
    while not stopping:
        for child in children:
            child.tend()
        time.sleep(1)
    log.info("stopping")
    for child in children:
        child.stop()
    for child in children:
        child.wait(10)


if __name__ == "__main__":
    main()
