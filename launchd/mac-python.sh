#!/bin/sh
# Give the Mac worker a Python of its own, so the macOS permissions it needs
# (Full Disk Access, Accessibility, Automation) are granted to a binary only
# Fulla runs, not to uv's shared interpreter that every `uv run` program uses.
#
# Copies a uv-managed CPython into ~/Library/Application Support/Fulla/python,
# re-signs it ad hoc under its own identifier, and builds the worker's venv on
# it in ~/Library/Application Support/Fulla/venv. Safe to run again: it keeps
# the copy (and so the grants) and only re-syncs the venv. To move to a new
# Python, delete the python directory, run this again and grant again.
set -eu
PATH="$HOME/.local/bin:$PATH"   # where uv's installer puts it
cd "$(dirname "$0")/.."

home="$HOME/Library/Application Support/Fulla"
version="${FULLA_PYTHON_VERSION:-3.13}"
python="$home/python/bin/python$version"

if [ ! -x "$python" ]; then
  uv python install -q "$version"
  shared=$(cd / && uv python find --no-project --managed-python "$version")
  src=$(dirname "$(dirname "$(realpath "$shared")")")
  mkdir -p "$home"
  rm -rf "$home/python.tmp"
  ditto "$src" "$home/python.tmp"
  # Its own code identity: a grant to this copy matches nothing else.
  codesign --force --sign - --identifier com.fulla.python "$home/python.tmp/bin/python$version" 2>/dev/null
  mv "$home/python.tmp" "$home/python"
fi

UV_PYTHON="$python" UV_PROJECT_ENVIRONMENT="$home/venv" uv sync -q

real=$(realpath "$home/venv/bin/python")
case "$real" in
  "$home"/*) ;;
  *) echo "the venv's python resolves to $real, outside $home" >&2; exit 1 ;;
esac
echo "Grant Full Disk Access, Accessibility and Automation to this binary only:"
echo "  $real"
