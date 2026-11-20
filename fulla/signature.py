"""Verify Norns's signed POSTs: serverless-worker wakes and run webhooks share
the Stripe scheme, `t=<unix>,v1=<hex hmac-sha256 over "<t>.<body>">`."""

from __future__ import annotations

import hashlib
import hmac
import time

TOLERANCE = 300
# Unix seconds are ten digits until 2286; a longer `t` is not a timestamp,
# and past Python's int-string limit int() would raise instead of answer.
MAX_T_DIGITS = 12


def verify(secret: str, body: bytes, header: str, *, now: float | None = None) -> bool:
    """Rejects an empty secret, a bad MAC, a malformed header, or a timestamp more than five
    minutes from now in either direction."""
    if not secret:  # an HMAC under an empty key is one anybody can compute
        return False
    parts = dict(p.split("=", 1) for p in header.split(",") if "=" in p)
    t, v1 = parts.get("t"), parts.get("v1")
    if not t or not v1 or not t.isascii() or not t.isdigit() or len(t) > MAX_T_DIGITS:
        return False
    if not v1.isascii():
        return False
    try:
        stamp = int(t)
    except ValueError:
        return False
    if abs((now if now is not None else time.time()) - stamp) > TOLERANCE:
        return False
    expected = hmac.new(secret.encode(), f"{t}.".encode() + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected.encode(), v1.encode())


def telegram_secret_token(norns_webhook_secret: str) -> str:
    """The token Telegram sends with each webhook update, derived from the run
    webhook secret so there is one secret to store, not two. 64 hex chars,
    within Telegram's allowed charset."""
    return hmac.new(norns_webhook_secret.encode(), b"telegram-webhook", hashlib.sha256).hexdigest()


def signal_secret_token(norns_webhook_secret: str) -> str:
    """The secret the Signal forwarder signs its POSTs to /signal with, derived
    the same way, so the signal machine holds a secret of its own and the
    webhook's is never copied there."""
    return hmac.new(norns_webhook_secret.encode(), b"signal-forwarder", hashlib.sha256).hexdigest()


def print_signal_secret():
    """`fulla-signal-secret`: the forwarder's secret, for `fly secrets set`
    on the signal app. Reads NORNS_WEBHOOK_SECRET."""
    import os

    print(signal_secret_token(os.environ["NORNS_WEBHOOK_SECRET"]))
