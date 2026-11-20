"""One HTTP surface for everything that can wake Fulla.

  POST /telegram       an update from Telegram's webhook, authenticated by
                       X-Telegram-Bot-Api-Secret-Token
  POST /signal         an envelope from the Signal forwarder (deploy/signal/),
                       X-Norns-Signature with the forwarder's derived secret
  POST /sms            an incoming text from Twilio's webhook, a form signed
                       with X-Twilio-Signature (the account's auth token over
                       the URL and the fields); answered with empty TwiML
  POST /norns/events   a run webhook delivery from Norns, X-Norns-Signature
  POST /norns/wake     a serverless-worker wake from Norns, X-Norns-Signature;
                       held open while the worker serves
  POST /hosts          a machine worker announcing itself (see hosts.py),
                       X-Norns-Signature with the Norns API key as the secret
  POST /confirm/claim  a machine claiming a payload the owner approved in
                       the chat (see confirm.py), signed like /hosts
  POST /egdir/hook     a Claude Code hook event from a box running egdir
                       (its egdir-notify), bearer EGDIR_NOTIFY_SECRET; a
                       question becomes one line to the owner, no run
  GET  /               health, and whether a worker incarnation is up

On Fly the machine starts for the first request and stops a few minutes
after the last one closes. A wake stays open for as long as the worker is
serving, so the machine lives exactly that long.
"""

from __future__ import annotations

import hmac
import json
import logging
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl

from fulla import sms
from fulla.channel import Channel, NotTaken
from fulla.confirm import ClaimRefused
from fulla.hosts import NameTaken
from fulla.signal import parse_envelope
from fulla.signature import verify
from fulla.telegram import parse_update
from fulla.wake import Incarnations

log = logging.getLogger("fulla.server")

TWIML_EMPTY = b'<?xml version="1.0" encoding="UTF-8"?><Response></Response>'


def make_handler(
    channel: Channel,
    *,
    telegram_secret: str,
    norns_secret: str,
    wake_secret: str = "",
    incarnations: Incarnations | None = None,
    worker_id: str = "fulla",
    host_secret: str = "",
    on_host: Callable[[dict], list[str]] | None = None,
    signal_secret: str = "",
    sms_auth_token: str = "",
    sms_url: str = "",
    egdir_secret: str = "",
):
    """Who may talk (the owner, listed friends, allowed groups) is the
    channel's decision; the routes authenticate the messenger and pass
    everything it parsed. `sms_url` is the address Twilio was told to POST
    to, which its signature covers."""
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self._json(200, {"ok": True, "worker_id": worker_id,
                             "awake": bool(incarnations and incarnations.awake)})

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("content-length", 0)))
            if self.path == "/telegram":
                self._telegram(body)
            elif self.path == "/signal" and signal_secret:
                self._signal(body)
            elif self.path == "/sms" and sms_auth_token and sms_url:
                self._sms(body)
            elif self.path == "/norns/events":
                self._norns_event(body)
            elif self.path == "/norns/wake" and incarnations is not None:
                self._norns_wake(body)
            elif self.path == "/hosts" and on_host is not None:
                self._host(body)
            elif self.path == "/confirm/claim" and host_secret:
                self._claim(body)
            elif self.path == "/egdir/hook" and egdir_secret:
                self._egdir(body)
            else:
                self._json(404, {"error": "not found"})

        def _host(self, body: bytes):
            if not host_secret or not verify(host_secret, body, self.headers.get("x-norns-signature", "")):
                log.warning("host announcement with bad signature from %s", self.client_address[0])
                self._json(401, {"error": "bad signature"})
                return
            try:
                data = json.loads(body or b"{}")
                known = on_host(data if isinstance(data, dict) else {})  # pyright: ignore[reportOptionalCall]
            except NameTaken as e:
                log.warning("host announcement refused: %s", e)
                self._json(409, {"error": str(e)})
                return
            except ValueError as e:
                self._json(400, {"error": str(e)})
                return
            except Exception:
                log.exception("handling host announcement failed")
                self._json(500, {"error": "handler failed"})
                return
            self._json(200, {"ok": True, "known": known})

        def _claim(self, body: bytes):
            if not verify(host_secret, body, self.headers.get("x-norns-signature", "")):
                log.warning("claim with bad signature from %s", self.client_address[0])
                self._json(401, {"error": "bad signature"})
                return
            try:
                data = json.loads(body or b"{}")
                claimed = channel.claim(str(data.get("id") or ""), str(data.get("digest") or ""))
            except ClaimRefused as e:
                log.warning("claim refused: %s", e)
                self._json(409, {"error": str(e)})
                return
            except (ValueError, AttributeError):
                self._json(400, {"error": "bad request"})
                return
            log.info("confirmation %s claimed", data.get("id"))
            self._json(200, claimed)

        def _egdir(self, body: bytes):
            """A box's Claude Code said something through its hook. A
            question or permission prompt (state waiting) is one line to
            the owner at the primary address; nothing runs. Answering is
            the manager's job when the owner replies, with the box's tmux
            tools. A finished turn is not relayed: the hook fires for every
            Claude session in tmux on the box, the owner's own included,
            and a line per turn would be noise."""
            given = self.headers.get("authorization", "")
            if not hmac.compare_digest(given, "Bearer " + egdir_secret):
                log.warning("egdir hook with bad bearer from %s", self.client_address[0])
                self._json(401, {"error": "bad bearer"})
                return
            try:
                event = json.loads(body or b"{}")
            except ValueError:
                self._json(400, {"error": "bad json"})
                return
            line = egdir_line(event if isinstance(event, dict) else {})
            if line is None:
                self._json(200, {"ok": True, "ignored": True})
                return
            log.info("egdir hook: %s", line)
            try:
                channel.tell_owner(line)
            except Exception:
                log.exception("telling the owner about an egdir event failed")
            self._json(200, {"ok": True})

        def _telegram(self, body: bytes):
            given = self.headers.get("x-telegram-bot-api-secret-token", "")
            if not telegram_secret or not hmac.compare_digest(given, telegram_secret):
                log.warning("telegram update with bad secret token (got %d chars, expected %d)",
                            len(given), len(telegram_secret))
                self._json(403, {"error": "forbidden"})
                return
            update = json.loads(body or b"{}")
            inbound = parse_update(update)
            if inbound is None:
                log.info("telegram update %s: not a text message, callback or reaction; ignored", update.get("update_id"))
                self._json(200, {"ok": True, "ignored": True})
                return
            log.info("telegram update %s: %s from %s", update.get("update_id"), inbound.kind, inbound.user_id)
            self._handle(inbound, "telegram update")

        def _handle(self, inbound, what: str):
            """Hand a message to the channel. 503 when Norns could not
            be reached, so the messenger brings the message again (Telegram
            retries for a day; the forwarder until it is taken). 200 on any
            other failure: a retry of a message we broke on would not go
            better, and the channel has logged it."""
            try:
                channel.handle(inbound)
            except NotTaken as e:
                log.warning("%s not taken: %s; asking for it again", what, e)
                self._json(503, {"error": "not taken", "retry": True})
                return
            except Exception:
                log.exception("handling %s failed", what)
            self._json(200, {"ok": True})

        def _signal(self, body: bytes):
            if not signal_secret or not verify(signal_secret, body, self.headers.get("x-norns-signature", "")):
                log.warning("signal envelope with bad signature from %s", self.client_address[0])
                self._json(401, {"error": "bad signature"})
                return
            payload = json.loads(body or b"{}")
            inbound = parse_envelope(payload if isinstance(payload, dict) else {})
            if inbound is None:
                dm = ((payload.get("envelope") or {}).get("dataMessage") or {}) if isinstance(payload, dict) else {}
                log.info("signal envelope: not a message or reaction (%s); ignored", ", ".join(sorted(dm)) or "empty")
                self._json(200, {"ok": True, "ignored": True})
                return
            log.info("signal envelope: %s from %s%s", inbound.kind, inbound.user_id,
                     f" in group {inbound.chat_id} ({inbound.chat_title or 'unnamed'})" if inbound.group else "")
            self._handle(inbound, "signal envelope")

        def _sms(self, body: bytes):
            """Twilio gives a message one try and, if that fails, one more at
            the fallback address, which `Sms.set_webhook` makes this same
            route: so 503 for a message Norns could not take, like the
            others, and TwiML with nothing in it for one that is handled,
            since a 200 with a body Twilio cannot read is logged as an
            error on its side."""
            params = parse_qsl(body.decode(errors="replace"), keep_blank_values=True)
            if not sms.verify_signature(sms_auth_token, sms_url, params, self.headers.get("x-twilio-signature", "")):
                log.warning("sms webhook with bad signature from %s", self.client_address[0])
                self._json(403, {"error": "forbidden"})
                return
            fields = dict(params)
            inbound = sms.parse_webhook(fields)
            if inbound is None:
                log.info("sms webhook: not an incoming message (%s); ignored", ", ".join(sorted(fields)) or "empty")
                self._twiml()
                return
            log.info("sms %s: text from %s", fields.get("MessageSid"), inbound.user_id)
            try:
                channel.handle(inbound)
            except NotTaken as e:
                log.warning("sms %s not taken: %s; asking for it again", fields.get("MessageSid"), e)
                self._json(503, {"error": "not taken", "retry": True})
                return
            except Exception:
                log.exception("handling sms failed")
            self._twiml()

        def _twiml(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/xml")
            self.send_header("Content-Length", str(len(TWIML_EMPTY)))
            self.end_headers()
            self.wfile.write(TWIML_EMPTY)

        def _norns_event(self, body: bytes):
            if not verify(norns_secret, body, self.headers.get("x-norns-signature", "")):
                log.warning("run event with bad signature from %s", self.client_address[0])
                self._json(401, {"error": "bad signature"})
                return
            event = json.loads(body or b"{}")
            log.info("run event: %s run %s", event.get("type"), event.get("run_id"))
            try:
                channel.handle_event(event)
            except Exception:
                log.exception("handling run event failed")
                self._json(500, {"error": "handler failed"})
                return
            self._json(200, {"ok": True})

        def _norns_wake(self, body: bytes):
            if not verify(wake_secret, body, self.headers.get("x-norns-signature", "")):
                log.warning("wake with bad signature from %s", self.client_address[0])
                self._json(401, {"error": "bad signature"})
                return
            log.info("wake received: %s", body.decode(errors="replace"))
            incarnations.wait_for_end_of_current()  # pyright: ignore[reportOptionalMemberAccess]
            self._json(200, {"ok": True})

        def _json(self, status: int, payload: dict):
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, fmt, *args):
            log.debug(fmt, *args)

    return Handler


def egdir_line(event: dict) -> str | None:
    """The one line the owner reads for a hook event, or None for one not
    worth a message. `state` is what the box's hook recorded after the
    event; only `waiting` (Claude asked something, and `message` is the
    question) is worth one. A finished turn, an idle notice, and an ended
    session are not: Claude ends a turn every time anyone on the box talks
    to it."""
    window = str(event.get("window") or "").strip()
    host = str(event.get("host") or "").strip()
    if not window or event.get("state") != "waiting":
        return None
    where = f"{window} on {host}" if host else window
    first = str(event.get("message") or event.get("title") or "").strip().splitlines()[0:1]
    message = first[0] if first else ""
    return f"{where} is waiting: {message}" if message else f"{where} is waiting for an answer"


def serve(channel: Channel, *, port: int, **kwargs) -> ThreadingHTTPServer:
    return ThreadingHTTPServer(("0.0.0.0", port), make_handler(channel, **kwargs))
