"""Turn signal-cli-rest-api's receive stream into signed POSTs to Fulla.

Runs beside signal-cli on the signal machine. Fulla sleeps between requests
and must not hold a websocket open, so this holds it instead and POSTs
each envelope to FULLA_URL/signal, signed Stripe-style with
FULLA_SIGNAL_SECRET (from `fulla-signal-secret`) in X-Norns-Signature, the
way Norns signs its own calls. It filters nothing: Fulla's server decides
whose messages count, as it does for Telegram.

Nothing is dropped. Signal's servers hand a message over once, so the
moment it arrives it is written to a spool on the volume, and a second
thread delivers the spool in order, oldest first, retrying each file until
Fulla answers 2xx. Fulla's machine takes several seconds to start and may
be down for a deploy; the owner's message waits on disk either way. Fulla
ignores a message it has already seen, so a retry after a lost reply is
harmless.

Environment: SIGNAL_NUMBER, FULLA_URL, FULLA_SIGNAL_SECRET; SIGNAL_API
(default http://127.0.0.1:8080); FORWARD_SPOOL (default under the volume).
Standard library plus `websockets`.
"""

import hashlib
import hmac
import itertools
import json
import logging
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

from websockets.sync.client import connect

logging.basicConfig(level=logging.INFO, format="%(asctime)s forward %(message)s", stream=sys.stdout)
log = logging.getLogger("forward")

SPOOL = "/home/.local/share/signal-cli/forward-spool"
POST_TIMEOUT = 120  # a cold Fulla machine answers in seconds; a deploy can take longer
MAX_BACKOFF = 60


def sign(secret: str, body: bytes) -> str:
    t = int(time.time())
    mac = hmac.new(secret.encode(), f"{t}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={t},v1={mac}"


def post(url: str, secret: str, body: bytes) -> bool:
    """One attempt. True once Fulla has answered 2xx. 401 means the secrets
    disagree; the file waits on disk until someone fixes that."""
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json", "X-Norns-Signature": sign(secret, body)})
    try:
        with urllib.request.urlopen(req, timeout=POST_TIMEOUT) as resp:
            resp.read()
        return True
    except urllib.error.HTTPError as e:
        if e.code == 401:
            log.error("Fulla rejected the signature; is FULLA_SIGNAL_SECRET right? Holding the message.")
        else:
            log.warning("POST %s: %s", url, e)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        log.warning("POST %s: %s", url, e)
    return False


class Spool:
    """Envelopes waiting for Fulla, one file each, named so that sorting the
    directory is arrival order. Written whole then renamed, so a crash
    mid-write leaves no half file to deliver."""

    def __init__(self, path: Path):
        self.path = path
        self.path.mkdir(parents=True, exist_ok=True)
        self._seq = itertools.count()
        for stray in self.path.glob("*.tmp"):
            stray.unlink()

    def put(self, body: bytes) -> Path:
        name = f"{time.time_ns():020d}-{next(self._seq):06d}.json"
        tmp = self.path / (name + ".tmp")
        tmp.write_bytes(body)
        final = self.path / name
        tmp.rename(final)
        return final

    def waiting(self) -> list[Path]:
        return sorted(self.path.glob("*.json"))


def deliver(spool: Spool, send: Callable[[bytes], bool], wake: threading.Event,
            stop: threading.Event, *, backoff: float = 2.0) -> None:
    """Drain the spool oldest first, forever. A file that will not go is
    retried with a widening wait, and nothing behind it is sent before it,
    so the owner's messages reach Fulla in the order they were sent."""
    wait = backoff
    while not stop.is_set():
        files = spool.waiting()
        if not files:
            wake.wait(timeout=30)
            wake.clear()
            continue
        head = files[0]
        try:
            body = head.read_bytes()
        except FileNotFoundError:
            continue
        if send(body):
            head.unlink(missing_ok=True)
            wait = backoff
            continue
        if len(files) > 1:
            log.warning("%d messages waiting for Fulla; retrying in %.0fs", len(files), wait)
        stop.wait(wait)
        wait = min(wait * 2, MAX_BACKOFF)


def interesting(payload: dict) -> bool:
    """Only data messages go to Fulla; receipts and typing are noise, and
    each would wake her machine."""
    env = payload.get("envelope") or {}
    return bool(env.get("dataMessage"))


def main() -> None:
    missing = [k for k in ("SIGNAL_NUMBER", "FULLA_URL", "FULLA_SIGNAL_SECRET") if not os.environ.get(k)]
    if missing:
        # Before the number is registered and the secrets set there is
        # nothing to do; s6 would restart a crash in a loop, so idle instead.
        log.error("not forwarding: %s not set", ", ".join(missing))
        time.sleep(3600)
        return
    number = os.environ["SIGNAL_NUMBER"]
    fulla = os.environ["FULLA_URL"].rstrip("/") + "/signal"
    secret = os.environ["FULLA_SIGNAL_SECRET"]
    api = os.environ.get("SIGNAL_API", "http://127.0.0.1:8080").rstrip("/")
    url = f"{api.replace('http', 'ws', 1)}/v1/receive/{number}"

    spool = Spool(Path(os.environ.get("FORWARD_SPOOL", SPOOL)))
    if spool.waiting():
        log.info("%d messages spooled from before; delivering first", len(spool.waiting()))
    wake, stop = threading.Event(), threading.Event()
    threading.Thread(target=deliver, args=(spool, lambda b: post(fulla, secret, b), wake, stop),
                     name="deliver", daemon=True).start()

    while True:
        try:
            with connect(url) as ws:
                log.info("receiving from %s, forwarding to %s", url, fulla)
                for raw in ws:
                    try:
                        payload = json.loads(raw)
                    except ValueError:
                        continue
                    if not isinstance(payload, dict) or not interesting(payload):
                        continue
                    spool.put(json.dumps(payload).encode())
                    wake.set()
        except Exception as e:  # the daemon starting up or restarting
            log.warning("receive stream failed: %s; reconnecting", e)
            time.sleep(5)


if __name__ == "__main__":
    main()
