"""Feedback: what the owner said Fulla could have done better.

Not a directive (she can't always act on it) and not a fact (it's about her,
not the world). A note for the humans: periodically the list is read and each
item becomes a prompt change, a new tool, a standing instruction, or a
won't-do, and is marked resolved with that outcome. The owner resolves one
by saying so in their chat (`resolve_feedback`) or with `fulla-feedback` on
the machine. A nightly shipping check, when Fulla's own repository is among
the owner's sources, notes which items a recent commit appears to address
(`mark_shipped`); the note closes nothing.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

OUTCOMES = ("prompt", "tool", "directive", "wontfix", "done")


@dataclass
class Feedback:
    id: int
    text: str
    context: str
    created_at: str
    resolved_at: str | None = None
    outcome: str | None = None
    note: str | None = None
    shipped: str | None = None  # "<commit> <summary>": a commit that looks like it answers this
    shipped_at: str | None = None


class FeedbackStore:
    def __init__(self, path: Path):
        self.path = path
        self._items: list[Feedback] = []
        if path.exists():
            self._items = [Feedback(**f) for f in json.loads(path.read_text() or "[]")]

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps([asdict(f) for f in self._items], indent=2))
        tmp.replace(self.path)

    def add(self, text: str, context: str) -> Feedback:
        next_id = max((f.id for f in self._items), default=0) + 1
        f = Feedback(id=next_id, text=text.strip(), context=context.strip(),
                     created_at=datetime.now(UTC).isoformat())
        self._items.append(f)
        self._save()
        return f

    def get(self, feedback_id: int) -> Feedback | None:
        return next((f for f in self._items if f.id == feedback_id), None)

    def mark_shipped(self, feedback_id: int, commit: str, summary: str) -> Feedback | None:
        """Note that `commit` appears to answer an open item; closes nothing."""
        f = self.get(feedback_id)
        if f is None or f.resolved_at is not None:
            return None
        f.shipped = f"{commit.strip()[:12]} {summary.strip()}".strip()
        f.shipped_at = datetime.now(UTC).isoformat()
        self._save()
        return f

    def resolve(self, feedback_id: int, outcome: str, note: str = "") -> Feedback | None:
        if outcome not in OUTCOMES:
            raise ValueError(f"outcome must be one of {', '.join(OUTCOMES)}")
        for f in self._items:
            if f.id == feedback_id:
                f.resolved_at = datetime.now(UTC).isoformat()
                f.outcome = outcome
                f.note = note.strip() or None
                self._save()
                return f
        return None

    def all(self) -> list[Feedback]:
        return list(self._items)

    def open(self) -> list[Feedback]:
        return [f for f in self._items if f.resolved_at is None]

    @staticmethod
    def render(items: list[Feedback]) -> str:
        if not items:
            return "No feedback."
        lines = []
        for f in items:
            when = f.created_at[:10]
            status = f" [{f.outcome}]" if f.outcome else ""
            lines.append(f"#{f.id} ({when}){status} {f.text}")
            if f.context:
                lines.append(f"    while: {f.context}")
            if f.shipped and not f.resolved_at:
                lines.append(f"    looks shipped ({f.shipped_at[:10]}, unconfirmed): {f.shipped}")  # pyright: ignore[reportOptionalSubscript]
            if f.note:
                lines.append(f"    note: {f.note}")
        return "\n".join(lines)
