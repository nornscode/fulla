"""Who Fulla is: its voice and character, in its own words, written with the owner.

A fact is about the world, a directive is a rule about what to do, feedback is
what went wrong. The soul is none of those: it is how Fulla talks and carries
itself (how blunt, how warm, how much it says, what it calls the owner,
whether it speaks up unasked). It lives in `soul.md` as a page of first-person
prose and is rendered into the manager's system prompt whole, so it is in front
of the model on every turn.

It starts unwritten. The prompt then carries a default voice and one
instruction: on the owner's first real message, ask how they want it to be,
and write it down. From then on it changes two ways: the owner asks ("be
warmer", "stop with the jokes"), or the nightly review notices something about
how they like to be dealt with and revises one line, marked as learned.

`fulla-soul show|set <file>|clear` works on the file directly, on the machine
that holds it.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

MAX_CHARS = 4000  # a page; the soul is a prompt section, not a memoir

DEFAULT_VOICE = """\
You are brief. One or two sentences unless a list is genuinely clearer. No \
preamble, no "great question", no offers to do more. If something is done, say \
what was done and stop."""

UNWRITTEN = """\
{default}

Your soul is unwritten: nobody has told you yet how {owner} wants you to be. \
On {owner}'s first message that does not begin with "[scheduled]", before \
anything else, introduce yourself in one line and ask, in one message, how they \
want you: what to call them, how blunt to be, how much to say, whether to speak \
up unprompted or wait to be asked, and anything about you they would enjoy. One \
message, not an interview in rounds. When they answer, call `write_soul` with a \
page in the first person that says who you are to them, in their terms, and \
read the gist back in two lines. If they would rather you worked it out ("you \
figure it out", "surprise me"), write a soul from the defaults above with the \
line "I am still learning how {owner} wants me; I revise this as I go." Then \
deal with whatever their first message asked for. A scheduled message never \
asks this; it uses the defaults above."""

WRITTEN = """\
{soul}

That is who you are, in your own words, written with {owner}. Hold to it in \
every reply, scheduled ones included. When {owner} asks you to be different in \
a lasting way ("be warmer", "stop with the jokes", "call me boss"), rewrite the \
part it bears on with `write_soul`, keeping the rest. A request for this one \
reply changes nothing. If {owner} wants to start over, ask the first questions \
again and write a new one."""


class Soul:
    def __init__(self, path: Path):
        self.path = path

    @property
    def text(self) -> str:
        return self.path.read_text().strip() if self.path.exists() else ""

    @property
    def written(self) -> bool:
        return bool(self.text)

    def write(self, text: str) -> None:
        text = text.strip()
        if not text:
            raise ValueError("a soul cannot be empty; use clear to start over")
        if len(text) > MAX_CHARS:
            raise ValueError(f"that is {len(text)} characters; a soul is at most {MAX_CHARS}. Shorten it.")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(text + "\n")
        tmp.replace(self.path)

    def clear(self) -> bool:
        if self.path.exists():
            self.path.unlink()
            return True
        return False

    def render(self, owner: str) -> str:
        """The prompt section: the soul and how to keep it, or the default voice
        and how to get one."""
        if self.written:
            return WRITTEN.format(soul=self.text, owner=owner)
        return UNWRITTEN.format(default=DEFAULT_VOICE, owner=owner)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("show")
    st = sub.add_parser("set", help="replace the soul with the contents of a file ('-' for stdin)")
    st.add_argument("file")
    sub.add_parser("clear", help="forget who Fulla is; it asks again on the next message")
    args = parser.parse_args(argv)

    soul = Soul(Path(os.environ.get("FULLA_DATA_DIR", "./data")) / "soul.md")
    if args.command == "show":
        print(soul.text if soul.written else "(unwritten)")
        return 0
    if args.command == "set":
        text = sys.stdin.read() if args.file == "-" else Path(args.file).read_text()
        try:
            soul.write(text)
        except ValueError as e:
            print(e, file=sys.stderr)
            return 1
        print("Written. It is in the prompt from the next time the worker joins; "
              "ask Fulla to `show_soul` to see it live.")
        return 0
    print("Cleared." if soul.clear() else "Nothing to clear.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
