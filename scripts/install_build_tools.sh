#!/usr/bin/env bash
# Installs cmake, git, and a C compiler -- the only real prerequisite for
# running from source. liboqs itself needs none of this documented
# separately: liboqs-python builds and installs it automatically on first
# `import oqs` using exactly these three tools, so once they're on PATH
# there's nothing else to set up.
set -euo pipefail

have() { command -v "$1" >/dev/null 2>&1; }

if have cmake && have git && (have cc || have gcc || have clang); then
  echo "cmake, git, and a C compiler are already on PATH. Nothing to do."
  exit 0
fi

if [ "$(uname)" = "Darwin" ]; then
  if ! have brew; then
    echo "Homebrew not found. Install it from https://brew.sh, then re-run this script." >&2
    exit 1
  fi
  brew install cmake git
  # Xcode Command Line Tools provide a C compiler on macOS; `brew install`
  # doesn't need to (and can't) install one itself.
  if ! (have cc || have clang); then
    xcode-select --install
  fi
elif have apt-get; then
  sudo apt-get update -qq
  sudo apt-get install -y cmake git build-essential
elif have dnf; then
  sudo dnf install -y cmake git gcc gcc-c++
elif have pacman; then
  sudo pacman -Sy --noconfirm cmake git base-devel
else
  echo "Unrecognized package manager. Install cmake, git, and a C compiler yourself." >&2
  exit 1
fi

echo "Done. liboqs will build itself automatically the first time you run lapsecoin."
