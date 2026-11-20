"""Standing instructions: how the owner wants Fulla to behave, from now on.

A fact is something the owner told Fulla about the world. A directive is
something they told her about herself: act like my manager, give me hard
feedback, never suggest mornings. Directives are rendered into the system
prompt, so they are in front of the model on every turn without being
recalled, and they can be changed from chat.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path


@dataclass
class Directive:
    id: int
    text: str
    created_at: str


class DirectiveStore:
    def __init__(self, path: Path):
        self.path = path
        self._items: list[Directive] = []
        if path.exists():
            self._items = [Directive(**d) for d in json.loads(path.read_text() or "[]")]

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps([asdict(d) for d in self._items], indent=2))
        tmp.replace(self.path)

    def add(self, text: str) -> Directive:
        next_id = max((d.id for d in self._items), default=0) + 1
        d = Directive(id=next_id, text=text.strip(), created_at=datetime.now(UTC).isoformat())
        self._items.append(d)
        self._save()
        return d

    def remove(self, directive_id: int) -> bool:
        before = len(self._items)
        self._items = [d for d in self._items if d.id != directive_id]
        if len(self._items) != before:
            self._save()
            return True
        return False

    def all(self) -> list[Directive]:
        return list(self._items)

    def render(self) -> str:
        """The prompt section. Numbered so the owner can refer to one by id."""
        if not self._items:
            return "(none yet)"
        return "\n".join(f"{d.id}. {d.text}" for d in self._items)
