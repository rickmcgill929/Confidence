#!/bin/bash
# Double-click in Finder to run Confidence.
# The first run creates a private Python environment in .venv and installs the
# app and its dependencies (PyQt6, python-osc, pyserial, pyobjc). If the
# Blackmagic DeckLink SDK is on your Desktop or in Downloads, it also builds the
# DeckLink bridge for SDI fill/key output. Later runs start instantly.
cd "$(dirname "$0")" || exit 1

PY=/usr/local/bin/python3          # python.org build
[ -x "$PY" ] || PY="$(command -v python3)"
if [ -z "$PY" ]; then
  echo "Python 3 not found. Install it from https://www.python.org/downloads/"
  read -n1 -p "Press any key to close."; exit 1
fi

if [ ! -x .venv/bin/confidence ] || [ pyproject.toml -nt .venv/.installed ]; then
  echo "Setting up Confidence (first run only)…"
  [ -d .venv ] || "$PY" -m venv .venv || { read -n1 -p "Could not create .venv. Press any key."; exit 1; }
  .venv/bin/python -m pip install --quiet --upgrade pip
  if ! .venv/bin/python -m pip install --quiet -e '.[clock]'; then
    read -n1 -p "Install failed (see above). Press any key to close."; exit 1
  fi
  touch .venv/.installed
fi

LIB=src/dsan/clock/native/libdsan_decklink.dylib
if [ ! -f "$LIB" ] || [ src/dsan/clock/native/dsan_decklink.cpp -nt "$LIB" ]; then
  packaging/build_decklink.sh >/dev/null 2>&1 && echo "Built DeckLink support." || true
fi

exec .venv/bin/confidence
