"""The screen in front of memory: text that would be fed back into a prompt
later must not carry instructions for the model.

A group's facts are written by its members and read back into the group
agent's prompt, so a line that says "ignore your instructions" or hides
text in invisible characters is refused at the door, with a reason the
tool can give. The patterns are deliberately conservative: a genuine fact
should never trip them. Borrowed from Mimir (mimir_agent/injection.py),
which screens its memory the same way.
"""

from __future__ import annotations

import re

_INSTRUCTION_OVERRIDE = re.compile(
    r"\b(?:ignore|disregard|forget|override)\s+(?:(?:all|any|the|your)\s+)?"
    r"(?:previous|prior|above|earlier|your)\s+"
    r"(?:instructions?|prompts?|directions?|rules?|context)\b",
    re.IGNORECASE,
)

_CONCEALMENT = re.compile(
    r"\bdo\s+not\s+(?:tell|inform|mention\s+(?:this\s+)?to|reveal\s+(?:this\s+)?to)\s+the\s+(?:user|owner)\b",
    re.IGNORECASE,
)

# Chat-template control tokens have no place in a stored fact.
_TEMPLATE_TOKENS = ("<|im_start|>", "<|im_end|>", "<|endoftext|>", "[INST]", "[/INST]", "<<SYS>>")

# The characters are spelled by code point so that none of them sits in
# this file as itself, where an editor would not show it.

# Bidi overrides (LRO, RLO) reorder displayed text; flag on first occurrence.
_BIDI_OVERRIDE = re.compile("[" + chr(0x202D) + chr(0x202E) + "]")

# The Unicode tag block, an invisible ASCII mirror used to hide text from
# human review. No legitimate use in a fact.
_TAG_CHARS = re.compile("[" + chr(0xE0000) + "-" + chr(0xE007F) + "]")

# Invisible characters that can hide text: zero-width space, word joiner
# and the invisible operators, byte-order mark. ZWJ and ZWNJ are left out:
# they occur in emoji sequences and in Arabic and Persian script, and a
# stray one or two survive copy-paste, so only a cluster is suspicious.
_HIDDEN_CHARS = re.compile("[" + chr(0x200B) + chr(0x2060) + "-" + chr(0x2064) + chr(0xFEFF) + "]")
_HIDDEN_CHAR_THRESHOLD = 3


def scan(text: str) -> str | None:
    """A short reason when `text` looks like a prompt injection, else None."""
    if _INSTRUCTION_OVERRIDE.search(text):
        return "an instruction-override phrase"
    if _CONCEALMENT.search(text):
        return "asks to conceal something from the owner"
    lowered = text.lower()
    for token in _TEMPLATE_TOKENS:
        if token.lower() in lowered:
            return f"the chat-template token {token}"
    if _TAG_CHARS.search(text):
        return "invisible Unicode tag characters"
    if _BIDI_OVERRIDE.search(text):
        return "bidirectional text override characters"
    if len(_HIDDEN_CHARS.findall(text)) >= _HIDDEN_CHAR_THRESHOLD:
        return "hidden zero-width characters"
    return None
