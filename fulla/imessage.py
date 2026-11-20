"""iMessage drafts as confirm cards (see confirm.py).

The manager drafts with `draft_imessage`; nothing is sent until the owner
taps Send on the card. Send starts a `fulla-mac-task` run that calls
`mac_imessage_deliver` with the draft's id and digest; the Mac claims the
approved payload from Fly and sends exactly that, once. If the Mac is off,
the run parks on "Waiting for the Mac." like any queued Mac task and goes
when the Mac reconnects.
"""

from __future__ import annotations

from fulla.confirm import Approval, Confirmation, ConfirmKind

KIND = "imessage"
DELIVER_TOOL = "mac_imessage_deliver"
RULE = "─" * 16


def payload(name: str, handle: str, text: str, why: str) -> dict:
    return {"name": name, "handle": handle, "text": text, "why": why}


def render(p: dict) -> str:
    lines = [f"iMessage to {p['name']} ({p['handle']})", RULE, p["text"], RULE]
    if p.get("why"):
        lines.append(f"Why: {p['why']}")
    lines.append("Nothing is sent until you choose Send.")
    return "\n".join(lines)


def revise(p: dict, text: str) -> dict:
    return {**p, "text": text}


def deliver_task(conf: Confirmation) -> str:
    return (f"Task: deliver the iMessage {conf.payload['name']} approved in the chat. Call "
            f"`{DELIVER_TOOL}` with draft_id \"{conf.id}\" and digest \"{conf.digest}\", exactly "
            f"as given.")


def kind(api, mac_task_agent: str = "fulla-mac-task") -> ConfirmKind:
    """The iMessage kind, approving through `api` (a NornsApi)."""

    def approve(conf: Confirmation) -> Approval:
        try:
            online = DELIVER_TOOL in api.tool_names()
        except Exception:
            online = False
        result = api.send_message(mac_task_agent, deliver_task(conf),
                                  conversation_key=f"imessage:{conf.id}")
        note = ("Sending from the Mac…" if online else
                "The Mac is off; it goes when the Mac is back.")
        return Approval(note=note, run_id=result.run_id)

    return ConfirmKind(name=KIND, render=render, approve=approve, revise=revise, claimable=True)
