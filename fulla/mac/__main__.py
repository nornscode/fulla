"""`python -m fulla.mac --env-file ~/.config/fulla/mac.env`: the Mac worker as
launchd runs it (`fulla-mac install` writes both the file and the plist).

launchd starts it on Fulla's own Python (launchd/mac-python.sh), not through
`uv run`, so the env file is read here, and before anything imports norns:
litellm loads the nearest .env on import, and the checkout's .env must not win
over the worker's. Imports nothing from fulla at module level for that reason;
`fulla.install` is standard library only and is imported inside the function.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path


def load_env_file(path: str) -> None:
    """KEY=value lines into the environment, as `uv run --env-file` does.
    A variable already set wins."""
    from fulla.install import parse_env

    for key, value in parse_env(Path(path)).items():
        os.environ.setdefault(key, value)


def run(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m fulla.mac")
    parser.add_argument("--env-file", help="KEY=value file to load first, e.g. ~/.config/fulla/mac.env")
    args = parser.parse_args(argv)
    if args.env_file:
        load_env_file(args.env_file)
    from fulla.mac.worker import main

    main()


if __name__ == "__main__":
    run()
