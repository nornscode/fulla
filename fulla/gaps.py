"""Gaps: things the owner asked for that Fulla cannot do.

Not feedback (a critique of how she worked, for the humans) and not a
directive. A gap is a missing capability: what the owner asked, in their
words, and one line of what was missing. Asked again, the same gap gains the
new words rather than a second entry, so how often it bites is on record.

The nightly review reads the open gaps and, for each, names the lowest rung
of the ladder (`RUNGS`, below) that would close it and proposes a fix. It
proposes only: nothing here starts a build or changes anything. When Fulla's
own repository is among the owner's sources, a nightly shipping check reads
its recent commits and notes which gaps one appears to address (`mark_shipped`):
a note, not a close, since a commit is a claim in someone else's words. A gap
closes on the owner's own say, through `resolve_gap` in their chat, or with
`fulla-feedback resolve-gap` on the machine.

A gap comes only from the owner's own request, never from an iMessage, a
web page or other text someone else wrote; the tool and the prompt say so.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

# The ladder, from the self-improvement design. The review picks the lowest that fits.
RUNGS = {
    1: "just do it: she had the tools and fumbled",
    2: "a directive or a line of the soul",
    3: "an agent defined from tools she has",
    4: "allowlist a tool that already exists",
    5: "a new tool, additive only",
    6: "a change to Fulla herself (or to Norns)",
}


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass
class Gap:
    id: int
    asked: list[str]
    missing: str
    created_at: str
    last_asked_at: str
    rung: int | None = None
    proposal: str | None = None
    proposed_at: str | None = None
    resolved_at: str | None = None
    note: str | None = None
    from_feedback: list[int] = field(default_factory=list)
    shipped: str | None = None  # "<commit> <summary>": a commit that looks like it closes this
    shipped_at: str | None = None

    @property
    def count(self) -> int:
        return len(self.asked)


class GapStore:
    def __init__(self, path: Path):
        self.path = path
        self._items: list[Gap] = []
        if path.exists():
            self._items = [Gap(**g) for g in json.loads(path.read_text() or "[]")]

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps([asdict(g) for g in self._items], indent=2))
        tmp.replace(self.path)

    def get(self, gap_id: int) -> Gap | None:
        return next((g for g in self._items if g.id == gap_id), None)

    def add(self, asked: str, missing: str, same_as: int = 0) -> Gap:
        """A new gap, or `asked` added to gap `same_as` when that one is open."""
        asked = asked.strip()
        same = self.get(same_as) if same_as else None
        if same is not None and same.resolved_at is None:
            same.asked.append(asked)
            same.last_asked_at = _now()
            if missing.strip() and not same.missing:
                same.missing = missing.strip()
            self._save()
            return same
        now = _now()
        g = Gap(id=max((g.id for g in self._items), default=0) + 1, asked=[asked],
                missing=missing.strip(), created_at=now, last_asked_at=now)
        self._items.append(g)
        self._save()
        return g

    def propose(self, gap_id: int, rung: int, proposal: str) -> Gap | None:
        if rung not in RUNGS:
            raise ValueError("rung must be 1 to 6")
        g = self.get(gap_id)
        if g is None or g.resolved_at is not None:
            return None
        g.rung, g.proposal, g.proposed_at = rung, proposal.strip(), _now()
        self._save()
        return g

    def mark_shipped(self, gap_id: int, commit: str, summary: str) -> Gap | None:
        """Note that `commit` appears to address an open gap. A note for the
        morning brief to ask about; it closes nothing."""
        g = self.get(gap_id)
        if g is None or g.resolved_at is not None:
            return None
        g.shipped, g.shipped_at = shipped_text(commit, summary), _now()
        self._save()
        return g

    def resolve(self, gap_id: int, note: str = "") -> Gap | None:
        g = self.get(gap_id)
        if g is None:
            return None
        g.resolved_at = _now()
        g.note = note.strip() or None
        self._save()
        return g

    def all(self) -> list[Gap]:
        return list(self._items)

    def open(self) -> list[Gap]:
        return [g for g in self._items if g.resolved_at is None]

    def migrate_feedback(self, feedback) -> list[Gap]:
        """Move open feedback that the old prompt filed as "capability I lack"
        into gaps, and resolve it as moved. Idempotent: moved feedback is
        resolved, so it is never seen twice."""
        moved = []
        for f in feedback.open():
            if "capability i lack" not in f.context.lower():
                continue
            now = _now()
            g = Gap(id=max((g.id for g in self._items), default=0) + 1, asked=[f.text],
                    missing="", created_at=f.created_at, last_asked_at=now,
                    from_feedback=[f.id])
            self._items.append(g)
            self._save()
            feedback.resolve(f.id, "done", f"moved to gap #{g.id}")
            moved.append(g)
        return moved

    @staticmethod
    def render(items: list[Gap]) -> str:
        if not items:
            return "No gaps."
        lines = []
        for g in items:
            times = f", asked {g.count} times" if g.count > 1 else ""
            status = " [closed]" if g.resolved_at else ""
            lines.append(f"#{g.id} ({g.created_at[:10]}{times}){status} missing: {g.missing or '(not said)'}")
            for a in g.asked:
                lines.append(f"    asked: {a}")
            if g.proposal:
                lines.append(f"    proposed {g.proposed_at[:10]}, rung {g.rung} ({RUNGS[g.rung]}): "  # pyright: ignore[reportArgumentType, reportOptionalSubscript]
                             f"{g.proposal}")
            if g.shipped and not g.resolved_at:
                lines.append(f"    looks shipped ({g.shipped_at[:10]}, unconfirmed): {g.shipped}")  # pyright: ignore[reportOptionalSubscript]
            if g.note:
                lines.append(f"    note: {g.note}")
        return "\n".join(lines)


def shipped_text(commit: str, summary: str) -> str:
    return f"{commit.strip()[:12]} {summary.strip()}".strip()
