"""Run AppleScript through `osascript`, with arguments passed as arguments.

The script is fixed text that reads `argv`; owner data (titles, handles,
message bodies) travels as separate process arguments and is never spliced
into script source, so a quote in a reminder title cannot change the script.
"""

from __future__ import annotations

import subprocess

TIMEOUT = 30


class AppleScriptError(Exception):
    pass


def run(script: str, *args: str, timeout: float = TIMEOUT) -> str:
    """Execute `script` with `args` as its `argv`; return what it returned."""
    try:
        proc = subprocess.run(
            ["osascript", "-", *args],
            check=False,
            input=script,
            text=True,
            capture_output=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        raise AppleScriptError("osascript is not available; this is not a Mac") from None
    except subprocess.TimeoutExpired:
        raise AppleScriptError(f"AppleScript did not finish within {timeout:.0f}s") from None
    if proc.returncode != 0:
        raise AppleScriptError(proc.stderr.strip() or f"osascript exited {proc.returncode}")
    return proc.stdout.rstrip("\n")
