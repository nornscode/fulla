"""Triage feedback: `fulla-feedback list [--all]`, `fulla-feedback resolve <id> --as <outcome> [--note ...]`.

Gaps, what the owner asked for that Fulla cannot do, with the nightly
review's proposed fix: `fulla-feedback gaps [--all]`, `fulla-feedback
resolve-gap <id> [--note ...]`.

Reads FULLA_DATA_DIR/feedback.json and gaps.json. In production that file is on the Fly
volume, so run it there:

  fly ssh console -a fulla -C "fulla-feedback list"
  fly ssh console -a fulla -C "fulla-feedback resolve 3 --as prompt --note 'brief now leads with today'"

Outcomes: prompt (the system prompt changed), tool (a tool was added or
changed), directive (it became a standing instruction), done (fixed some
other way), wontfix.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from fulla.feedback import OUTCOMES, FeedbackStore
from fulla.gaps import GapStore


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    ls = sub.add_parser("list")
    ls.add_argument("--all", action="store_true", help="include resolved items")
    rs = sub.add_parser("resolve")
    rs.add_argument("id", type=int)
    rs.add_argument("--as", dest="outcome", required=True, choices=OUTCOMES)
    rs.add_argument("--note", default="")
    gl = sub.add_parser("gaps")
    gl.add_argument("--all", action="store_true", help="include closed gaps")
    gr = sub.add_parser("resolve-gap")
    gr.add_argument("id", type=int)
    gr.add_argument("--note", default="")
    args = parser.parse_args(argv)

    data_dir = Path(os.environ.get("FULLA_DATA_DIR", "./data"))
    if args.command in ("gaps", "resolve-gap"):
        gaps = GapStore(data_dir / "gaps.json")
        if args.command == "gaps":
            print(GapStore.render(gaps.all() if args.all else gaps.open()))
            return 0
        g = gaps.resolve(args.id, args.note)
        if g is None:
            print(f"No gap #{args.id}.", file=sys.stderr)
            return 1
        print(f"Gap #{g.id} closed.")
        return 0
    store = FeedbackStore(data_dir / "feedback.json")
    if args.command == "list":
        print(FeedbackStore.render(store.all() if args.all else store.open()))
        return 0
    f = store.resolve(args.id, args.outcome, args.note)
    if f is None:
        print(f"No feedback #{args.id}.", file=sys.stderr)
        return 1
    print(f"#{f.id} resolved as {f.outcome}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
