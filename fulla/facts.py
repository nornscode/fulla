"""The facts store: things you told Fulla. A JSON file, nothing cleverer yet."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path


@dataclass
class Fact:
    id: int
    text: str
    created_at: str


class FactStore:
    def __init__(self, path: Path):
        self.path = path
        self._facts: list[Fact] = []
        self._load()

    def _load(self):
        if self.path.exists():
            raw = json.loads(self.path.read_text() or "[]")
            self._facts = [Fact(**f) for f in raw]

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps([asdict(f) for f in self._facts], indent=2))
        tmp.replace(self.path)

    def add(self, text: str) -> Fact:
        next_id = max((f.id for f in self._facts), default=0) + 1
        fact = Fact(id=next_id, text=text.strip(), created_at=datetime.now(UTC).isoformat())
        self._facts.append(fact)
        self._save()
        return fact

    def remove(self, fact_id: int) -> bool:
        before = len(self._facts)
        self._facts = [f for f in self._facts if f.id != fact_id]
        if len(self._facts) != before:
            self._save()
            return True
        return False

    def all(self) -> list[Fact]:
        return list(self._facts)

    def search(self, query: str) -> list[Fact]:
        """Every fact containing every word of the query, case-insensitive."""
        words = [w.lower() for w in query.split() if w.strip()]
        if not words:
            return self.all()
        return [f for f in self._facts if all(w in f.text.lower() for w in words)]
