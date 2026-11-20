"""Confirm cards: something Fulla proposed that happens only when the owner
approves it in the chat.

A proposal is a kind and a payload. The channel posts it as a card with
Send / Edit / Cancel (buttons on Telegram; on Signal a line of labels, and a
quoted reply naming one stands in for the tap, and a cancel
reaction for Cancel) and pins the
payload by digest: each card's buttons carry the first characters of the
digest they were shown with, so a tap on an older card (before an edit)
does nothing. Only the owner decides a card, in their own one-to-one chat
at the address it was posted to; the channel checks who and where. Approval
is one-shot: a
confirmation moves pending -> approved -> claimed -> done, and each step is
taken once, under the channel's lock, and saved before anything acts on it.

A kind says how to show a payload, how to apply an owner's edit, and what
approving it does (`ConfirmKind`). Approving may start a Norns run whose
outcome is written back onto the card, or finish at once with a note.

When the action runs on another machine (the Mac sends iMessages), that
machine claims the approved payload from Fly with `claim`, by id and
digest. Only an approved, unclaimed confirmation is handed out, and only
once, so the model that carries the id can trigger the action but never
choose its content.

Depends on nothing of Fulla's but `hosts.sign`, so a machine worker can
import it.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import asdict, dataclass, field

import httpx

from fulla.hosts import sign

PENDING, EDITING, APPROVED, CLAIMED, DONE, CANCELLED = (
    "pending", "editing", "approved", "claimed", "done", "cancelled")
OPEN = (PENDING, EDITING)  # the owner has not decided yet
DIGEST_SHOWN = 8  # digest characters carried in a button's callback data


def digest(kind: str, payload: dict) -> str:
    """The pin on a payload: sha256 over the kind and its canonical JSON."""
    canon = json.dumps({"kind": kind, "payload": payload}, sort_keys=True, ensure_ascii=False,
                       separators=(",", ":"))
    return hashlib.sha256(canon.encode()).hexdigest()


@dataclass
class Confirmation:
    id: str
    kind: str
    payload: dict
    digest: str
    status: str = PENDING
    message_id: int | None = None  # the card currently showing it, at `address`
    old_message_ids: list = field(default_factory=list)  # cards an edit retired, newest last
    # Where the card is: the owner's own one-to-one chat ("telegram:7",
    # "signal:+1…"). Only the owner, there, can decide it.
    address: str = ""
    run_id: int | None = None  # the run approving it started, if any
    created_at: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Approval:
    """What approving did: started a run (its outcome lands on the card
    later), and a note for the card meanwhile."""
    note: str
    run_id: int | None = None


@dataclass
class ConfirmKind:
    name: str
    render: Callable[[dict], str]  # the card's text; shown verbatim, no Markdown
    approve: Callable[[Confirmation], Approval]  # called once, after the approval is saved
    revise: Callable[[dict, str], dict] | None = None  # the owner's edit applied; None: no Edit button
    claimable: bool = False  # another machine claims the approved payload with `claim`
    approve_label: str = "Send"  # the approve button's text, and the word a reply says instead


class ClaimRefused(Exception):
    pass


def claim(url: str, api_key: str, confirm_id: str, pinned: str, *, timeout: float = 20.0) -> dict:
    """The approved payload, from Fly, for a machine about to act on it.
    Raises ClaimRefused when it is not approved, already claimed, or the
    digest does not match; the payload is checked against the digest here too."""
    if not url:
        raise ClaimRefused("FULLA_URL is not set on this machine")
    body = json.dumps({"id": confirm_id, "digest": pinned}).encode()
    try:
        resp = httpx.post(url.rstrip("/") + "/confirm/claim", content=body, timeout=timeout,
                          headers={"Content-Type": "application/json",
                                   "X-Norns-Signature": sign(api_key, body)})
    except httpx.HTTPError as e:
        raise ClaimRefused(f"could not reach Fulla: {e}") from e
    if resp.status_code != 200:
        try:
            reason = resp.json().get("error") or resp.text
        except ValueError:
            reason = resp.text
        raise ClaimRefused(str(reason)[:200])
    data = resp.json()
    kind, payload = data.get("kind"), data.get("payload")
    if not isinstance(payload, dict) or digest(kind, payload) != pinned:
        raise ClaimRefused("the payload does not match what was approved")
    return payload
